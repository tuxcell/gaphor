"""Parse a SysML Gaphor Model and generate a SysML data model."""

from typing import List, Set

from gaphor import UML
from gaphor.application import Session
from gaphor.storage import storage


def generate(filename, outfile=None, overridesfile=None):
    services = [
        "element_dispatcher",
        "event_manager",
        "component_registry",
        "element_factory",
        "modeling_language",
    ]
    session = Session(services=services)
    element_factory = session.get_service("element_factory")
    modeling_language = session.get_service("modeling_language")
    with open(filename):
        storage.load(
            filename, factory=element_factory, modeling_language=modeling_language,
        )
    with open(outfile, "w") as f:
        # Put imports at the top
        f.write(f"from gaphor.UML import Element\n")

        klass_names: Set = set()
        uml_names: List = dir(UML.uml)
        for klass in element_factory.select(lambda e: e.isKindOf(UML.Class)):
            name = klass.name
            if name in uml_names:
                f.write(f"from gaphor.UML import {name}\n")
            elif name not in klass_names and name[0] != "~":
                print(f"{name} with owned elements: {klass.ownedElement}")
                f.write(f"class {name}:\n")
                f.write("    pass\n\n")
                klass_names.add(name)

    element_factory.shutdown()
    session.shutdown()