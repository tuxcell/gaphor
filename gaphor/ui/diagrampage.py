import functools
import importlib
import logging
from typing import Dict, Optional, Sequence, Tuple

from gaphas.freehand import FreeHandPainter
from gaphas.painter import (
    BoundingBoxPainter,
    FocusedItemPainter,
    HandlePainter,
    PainterChain,
    ToolPainter,
)
from gaphas.view import GtkView
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from gaphor.core import action, event_handler, gettext, transactional
from gaphor.core.modeling import Presentation, StyleSheet
from gaphor.core.modeling.diagram import StyledDiagram
from gaphor.core.modeling.event import AttributeUpdated, ElementDeleted
from gaphor.diagram.diagramtoolbox import ToolDef
from gaphor.diagram.diagramtools import (
    DefaultTool,
    PlacementTool,
    TransactionalToolChain,
)
from gaphor.diagram.event import DiagramItemPlaced
from gaphor.diagram.painter import ItemPainter
from gaphor.diagram.support import get_diagram_item
from gaphor.transaction import Transaction
from gaphor.ui.actiongroup import create_action_group
from gaphor.ui.event import DiagramSelectionChanged

log = logging.getLogger(__name__)


def tooliter(toolbox_actions: Sequence[Tuple[str, Sequence[ToolDef]]]):
    """Iterate toolbox items, regardless of section headers."""
    for name, section in toolbox_actions:
        yield from section


with importlib.resources.path("gaphor.ui", "placement-icon-base.png") as f:
    PLACEMENT_BASE = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(f), 64, 64, True)

GtkView.set_css_name("diagramview")

_placement_pixbuf_map: Dict[str, GdkPixbuf.Pixbuf] = {}


_upper_offset = ord("A") - ord("a")


@functools.lru_cache(maxsize=None)
def parse_shortcut(shortcut):
    key, mod = Gtk.accelerator_parse(shortcut)
    return (key, key + _upper_offset), mod


def get_placement_cursor(display, icon_name):
    if icon_name in _placement_pixbuf_map:
        pixbuf = _placement_pixbuf_map[icon_name]
    else:
        pixbuf = PLACEMENT_BASE.copy()
        icon = Gtk.IconTheme.get_default().load_icon(icon_name, 24, 0)
        icon.copy_area(
            0, 0, icon.get_width(), icon.get_height(), pixbuf, 9, 15,
        )
        _placement_pixbuf_map[icon_name] = pixbuf
    return Gdk.Cursor.new_from_pixbuf(display, pixbuf, 1, 1)


class DiagramPage:

    VIEW_TARGET_STRING = 0
    VIEW_TARGET_ELEMENT_ID = 1
    VIEW_TARGET_TOOLBOX_ACTION = 2
    VIEW_DND_TARGETS = [
        Gtk.TargetEntry.new("gaphor/element-id", 0, VIEW_TARGET_ELEMENT_ID),
        Gtk.TargetEntry.new("gaphor/toolbox-action", 0, VIEW_TARGET_TOOLBOX_ACTION),
    ]

    def __init__(
        self, diagram, event_manager, element_factory, properties, modeling_language
    ):
        self.event_manager = event_manager
        self.element_factory = element_factory
        self.properties = properties
        self.diagram = diagram
        self.modeling_language = modeling_language

        self.view: Optional[GtkView] = None
        self.widget: Optional[Gtk.Widget] = None
        self.diagram_css: Optional[Gtk.CssProvider] = None

        self.event_manager.subscribe(self._on_element_delete)
        self.event_manager.subscribe(self._on_style_sheet_updated)
        self.event_manager.subscribe(self._on_diagram_item_placed)

    title = property(lambda s: s.diagram and s.diagram.name or gettext("<None>"))

    def get_diagram(self):
        return self.diagram

    def get_view(self):
        return self.view

    def construct(self):
        """Create the widget.

        Returns: the newly created widget.
        """
        assert self.diagram

        view = GtkView(canvas=self.diagram.canvas)
        view.drag_dest_set(
            Gtk.DestDefaults.ALL,
            DiagramPage.VIEW_DND_TARGETS,
            Gdk.DragAction.MOVE | Gdk.DragAction.COPY | Gdk.DragAction.LINK,
        )
        self.diagram_css = Gtk.CssProvider.new()
        view.get_style_context().add_provider(
            self.diagram_css, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_shadow_type(Gtk.ShadowType.IN)
        scrolled_window.add(view)
        scrolled_window.show_all()
        self.widget = scrolled_window

        view.connect("focus-changed", self._on_view_selection_changed)
        view.connect("selection-changed", self._on_view_selection_changed)
        view.connect_after("key-press-event", self._on_key_press_event)
        view.connect("drag-data-received", self._on_drag_data_received)
        view.connect("scroll-event", self.on_scroll)

        self.view = view

        self.widget.action_group = create_action_group(self, "diagram")

        self.widget.connect_after("key-press-event", self._on_shortcut_action)
        self.select_tool("toolbox-pointer")

        self.set_drawing_style()

        return self.widget

    def get_tool(self, tool_name):
        """Return a tool associated with an id (action name)."""
        if tool_name == "toolbox-pointer":
            return DefaultTool(self.event_manager)

        tool = next(
            t
            for t in tooliter(self.modeling_language.toolbox_definition)
            if t.id == tool_name
        )
        item_factory = tool.item_factory
        handle_index = tool.handle_index
        return PlacementTool(
            self.view,
            item_factory=item_factory,
            event_manager=self.event_manager,
            handle_index=handle_index,
        )

    def get_tool_icon_name(self, tool_name):
        if tool_name == "toolbox-pointer":
            return None
        return next(
            t
            for t in tooliter(self.modeling_language.toolbox_definition)
            if t.id == tool_name
        ).icon_name

    @event_handler(ElementDeleted)
    def _on_element_delete(self, event: ElementDeleted):
        if event.element is self.diagram:
            self.close()

    @event_handler(AttributeUpdated)
    def _on_style_sheet_updated(self, event: AttributeUpdated):
        if event.property is StyleSheet.styleSheet:
            self.set_drawing_style()

            canvas = self.diagram.canvas
            for item in canvas.get_all_items():
                canvas.request_update(item)

    def close(self):
        """Tab is destroyed.

        Do the same thing that would be done if Close was pressed.
        """
        assert self.widget
        self.widget.destroy()
        self.event_manager.unsubscribe(self._on_element_delete)
        self.event_manager.unsubscribe(self._on_style_sheet_updated)
        self.event_manager.unsubscribe(self._on_diagram_item_placed)
        self.view = None

    @action(
        name="diagram.zoom-in", shortcut="<Primary>plus",
    )
    def zoom_in(self):
        assert self.view
        self.view.zoom(1.2)

    @action(
        name="diagram.zoom-out", shortcut="<Primary>minus",
    )
    def zoom_out(self):
        assert self.view
        self.view.zoom(1 / 1.2)

    @action(
        name="diagram.zoom-100", shortcut="<Primary>0",
    )
    def zoom_100(self):
        assert self.view
        zx = self.view.matrix[0]
        self.view.zoom(1 / zx)

    @action(
        name="diagram.select-all", shortcut="<Primary>a",
    )
    def select_all(self):
        assert self.view
        if self.view.has_focus():
            self.view.select_all()

    @action(name="diagram.unselect-all", shortcut="<Primary><Shift>a")
    def unselect_all(self):
        assert self.view
        if self.view.has_focus():
            self.view.unselect_all()

    @action(name="diagram.delete")
    @transactional
    def delete_selected_items(self):
        assert self.view
        items = self.view.selected_items
        for i in list(items):
            if isinstance(i, Presentation):
                i.unlink()
            else:
                if i.canvas:
                    i.canvas.remove(i)

    @action(name="diagram.select-tool", state="toolbox-pointer")
    def select_tool(self, tool_name: str):
        if self.view:
            tool = TransactionalToolChain(self.event_manager)
            tool.append(self.get_tool(tool_name))
            self.view.tool = tool
            icon_name = self.get_tool_icon_name(tool_name)
            window = self.view.get_window()
            if icon_name and window:
                window.set_cursor(get_placement_cursor(window.get_display(), icon_name))
            elif window:
                window.set_cursor(None)

    @event_handler(DiagramItemPlaced)
    def _on_diagram_item_placed(self, event):
        assert self.widget
        if self.properties("reset-tool-after-create", True):
            self.widget.action_group.actions.lookup_action("select-tool").activate(
                GLib.Variant.new_string("toolbox-pointer")
            )

    def set_drawing_style(self):
        """Set the drawing style for the diagram based on the active style
        sheet."""
        assert self.view
        assert self.diagram_css

        style = self.diagram.style(StyledDiagram(self.diagram, self.view))

        bg = style.get("background-color")
        self.diagram_css.load_from_data(
            f"diagramview {{ background-color: rgba({int(255*bg[0])}, {int(255*bg[1])}, {int(255*bg[2])}, {bg[3]}) }}".encode()
            if bg
            else "".encode()
        )

        sloppiness = style.get("line-style", 0.0)

        item_painter = ItemPainter()

        view = self.view

        if sloppiness:
            item_painter = FreeHandPainter(ItemPainter(), sloppiness=sloppiness)

        view.painter = (
            PainterChain()
            .append(item_painter)
            .append(HandlePainter())
            .append(FocusedItemPainter())
            .append(ToolPainter())
        )
        view.bounding_box_painter = BoundingBoxPainter(item_painter)

        view.queue_draw_refresh()

    def on_scroll(self, btn, event):
        """Handle the mouse wheel to zoom in and out"""
        if event.direction == Gdk.ScrollDirection.UP:
            self.zoom_in()
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.zoom_out()
        return True 

    def _on_key_press_event(self, view, event):
        """Handle the 'Delete' key.

        This can not be handled directly (through GTK's accelerators),
        otherwise this key will confuse the text edit stuff.
        """
        if (
            view.is_focus()
            and event.keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace)
            and (
                event.get_state() == 0 or event.get_state() & Gdk.ModifierType.MOD2_MASK
            )
        ):
            self.delete_selected_items()

    def _on_shortcut_action(self, widget, event):
        # accelerator keys are lower case. Since we handle them in a key-press event
        # handler, we'll need the upper-case versions as well in case Shift is pressed.
        for _title, items in self.modeling_language.toolbox_definition:
            for action_name, _label, _icon_name, shortcut, *rest in items:
                if not shortcut:
                    continue
                keys, mod = parse_shortcut(shortcut)
                if event.state == mod and event.keyval in keys:
                    widget.get_toplevel().get_action_group("diagram").lookup_action(
                        "select-tool"
                    ).change_state(GLib.Variant.new_string(action_name))

    def _on_view_selection_changed(self, view, selection_or_focus):
        self.event_manager.handle(
            DiagramSelectionChanged(view, view.focused_item, view.selected_items)
        )

    def _on_drag_data_received(self, view, context, x, y, data, info, time):
        """Handle data dropped on the canvas."""
        if (
            data
            and data.get_format() == 8
            and info == DiagramPage.VIEW_TARGET_TOOLBOX_ACTION
        ):
            tool = self.get_tool(data.get_data().decode())
            tool.create_item((x, y))
            context.finish(True, False, time)
        elif (
            data
            and data.get_format() == 8
            and info == DiagramPage.VIEW_TARGET_ELEMENT_ID
        ):
            element_id = data.get_data().decode()
            element = self.element_factory.lookup(element_id)
            assert element

            item_class = get_diagram_item(type(element))
            if item_class:
                with Transaction(self.event_manager):
                    item = self.diagram.create(item_class)
                    assert item

                    x, y = view.get_matrix_v2i(item).transform_point(x, y)
                    item.matrix.translate(x, y)
                    item.subject = element

                view.unselect_all()
                view.focused_item = item

            else:
                log.warning(
                    f"No graphical representation for element {type(element).__name__}"
                )
            context.finish(True, False, time)
        else:
            context.finish(False, False, time)
