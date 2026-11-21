"""The canned A2UI surface: shape, catalog, and order.

The JavaScript renderer validates these envelopes when they arrive, so a wrong
component name or a dangling child reference is a *rendering* bug that no Python
type checker would catch. These tests pin the parts the renderer is strict about:

* component names must exist in the basic catalog (v1.0, 18 components);
* ids must be UAX #31 identifiers -- ``summary-text`` is rejected, ``summaryText``
  is not, and that rule is not in the A2UI prose;
* ``createSurface`` must precede ``updateComponents`` for the same surface.
"""

from __future__ import annotations

import re
from typing import Any

from quartz.web.a2ui_fixture import BASIC_CATALOG_ID, SURFACE_ID, canned_surface

BASIC_CATALOG_V1_COMPONENTS = frozenset(
    {
        "AudioPlayer",
        "Button",
        "Card",
        "CheckBox",
        "ChoicePicker",
        "Column",
        "DateTimeInput",
        "Divider",
        "Icon",
        "Image",
        "List",
        "Modal",
        "Row",
        "Slider",
        "Tabs",
        "Text",
        "TextField",
        "Video",
    }
)
"""`basicCatalog.components` in @a2ui/web_core 0.15.0 (the v1.0 catalog)."""

UAX31_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def components(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for envelope in messages:
        if "updateComponents" in envelope:
            return list(envelope["updateComponents"]["components"])
    return []


def test_the_surface_is_created_before_it_is_filled() -> None:
    messages = canned_surface()

    kinds = [next(iter(envelope.keys() - {"version"})) for envelope in messages]
    assert kinds == ["createSurface", "updateComponents"]
    assert all(envelope["version"] == "1.0" for envelope in messages)
    assert messages[0]["createSurface"]["surfaceId"] == SURFACE_ID
    assert messages[0]["createSurface"]["catalogId"] == BASIC_CATALOG_ID
    assert messages[1]["updateComponents"]["surfaceId"] == SURFACE_ID


def test_only_basic_catalog_components_are_used() -> None:
    names = {component["component"] for component in components(canned_surface())}
    assert names, "a surface with no components renders as a blank area"
    assert names <= BASIC_CATALOG_V1_COMPONENTS, names - BASIC_CATALOG_V1_COMPONENTS


def test_component_ids_are_identifiers_and_references_resolve() -> None:
    entries = components(canned_surface())
    ids = [entry["id"] for entry in entries]

    assert len(ids) == len(set(ids)), "the renderer rejects duplicate component ids"
    for component_id in ids:
        assert UAX31_IDENTIFIER.match(component_id), (
            f"{component_id!r} is not a UAX #31 identifier; A2UI rejects it at render time"
        )
    assert "root" in ids, "the surface root is always the component with id 'root'"

    known = set(ids)
    for entry in entries:
        children: list[str] = []
        if isinstance(entry.get("children"), list):
            children = list(entry["children"])
        elif isinstance(entry.get("child"), str):
            children = [entry["child"]]
        assert set(children) <= known, f"{entry['id']} references a missing component"


def test_the_fixture_is_stable_between_calls() -> None:
    """A pure function: two calls must not share mutable state."""
    first = canned_surface()
    second = canned_surface()
    assert first == second
    assert first is not second
