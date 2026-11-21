"""A canned A2UI v1.0 surface, emitted by the `a2ui` frame.

Step 1 of the A2UI work: prove that a surface built on the server actually paints in
the browser. This module therefore contains **no model call and no rendering** -- it
returns a fixed, ordered list of A2UI v1.0 wire envelopes that a real renderer must
accept. Real generation (the Python agent SDK driving Quartz's provider) replaces the
caller of `canned_surface()`, not the wire shape.

Three deliberate choices:

1. **Raw envelopes, not the Express DSL.** The spike's trap #1 is that a missing
   ``<a2ui>`` sentinel compiles to silence; a hand-written envelope list cannot fall
   into that hole.
2. **Basic catalog only.** ``@a2ui/web_core``'s ``basicCatalog`` is the one catalog the
   JavaScript renderer ships, so a component name that is not in it is a rendering bug,
   not a version mismatch. The Quartz catalog is the follow-up PR.
3. **Order is part of the payload.** ``createSurface`` must precede ``updateComponents``;
   the frame carries the sequence whole (see `docs/web-protocol.md`) because a consumer
   that drops or reorders one envelope is left with a half-built surface.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BASIC_CATALOG_ID", "SURFACE_ID", "canned_surface"]

SURFACE_ID = "quartz-dashboard"
"""The one surface this fixture names. The browser renders whatever id arrives."""

BASIC_CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
"""`basicCatalog.id` from @a2ui/web_core 0.15.0 -- the catalog the surface draws with."""

_TITLE = "**Quartz dashboard**"
"""Markdown, because the basic catalog's `Text` renders it. Plain text would also work."""


def canned_surface() -> list[dict[str, Any]]:
    """The ordered envelopes that build one small dashboard surface.

    Root is always the component with id ``root``: the renderer resolves the surface
    from it, so an id typo here shows as an empty surface rather than an error.

    Components are basic-catalog only (``Text``, ``Column``, ``Card``) and every prop
    is a name that catalog declares -- its schemas are strict, so an invented prop is
    a rejected component.
    """
    return [
        {
            "version": "1.0",
            "createSurface": {
                "surfaceId": SURFACE_ID,
                "catalogId": BASIC_CATALOG_ID,
            },
        },
        {
            "version": "1.0",
            "updateComponents": {
                "surfaceId": SURFACE_ID,
                "components": [
                    {
                        "id": "root",
                        "component": "Column",
                        "justify": "start",
                        "align": "stretch",
                        "children": ["heading", "summary", "caption"],
                    },
                    {
                        "id": "heading",
                        "component": "Text",
                        "text": _TITLE,
                    },
                    {
                        "id": "summary",
                        "component": "Card",
                        "child": "summaryText",
                    },
                    {
                        "id": "summaryText",
                        "component": "Text",
                        "text": (
                            "This surface was built on the server as A2UI v1.0 "
                            "envelopes and painted by `@a2ui/react`.\n\n"
                            "It is **canned** -- no model chose these components -- "
                            "so it proves the renderer, not the generator."
                        ),
                    },
                    {
                        "id": "caption",
                        "component": "Text",
                        "variant": "caption",
                        "text": "basic catalog · Quartz web · step 1 of 3",
                    },
                ],
            },
        },
    ]
