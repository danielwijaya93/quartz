/**
 * The canned A2UI surface the mock server emits, and the fixture the component test
 * renders. Mirrors `src/quartz/web/a2ui_fixture.py`, which is the server-side source
 * of truth: the point of the paint check is that *these* envelopes render.
 *
 * Basic catalog only (`Text`, `Column`, `Card`), raw v1.0 wire envelopes, ordered:
 * `createSurface` before `updateComponents`, root component id `root`.
 */

export const SURFACE_ID = "quartz-dashboard";

export const BASIC_CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json";

export function cannedSurface() {
  return [
    {
      version: "1.0",
      createSurface: { surfaceId: SURFACE_ID, catalogId: BASIC_CATALOG_ID },
    },
    {
      version: "1.0",
      updateComponents: {
        surfaceId: SURFACE_ID,
        components: [
          {
            id: "root",
            component: "Column",
            justify: "start",
            align: "stretch",
            children: ["heading", "summary", "caption"],
          },
          { id: "heading", component: "Text", text: "**Quartz dashboard**" },
          { id: "summary", component: "Card", child: "summaryText" },
          {
            id: "summaryText",
            component: "Text",
            text:
              "This surface was built on the server as A2UI v1.0 envelopes and painted " +
              "by `@a2ui/react`.\n\nIt is **canned** -- no model chose these components " +
              "-- so it proves the renderer, not the generator.",
          },
          {
            id: "caption",
            component: "Text",
            variant: "caption",
            text: "basic catalog \u00b7 Quartz web \u00b7 step 1 of 3",
          },
        ],
      },
    },
  ];
}

/** A frame shaped exactly as `docs/web-protocol.md` defines it. */
export function a2uiFrame(turnId = "turn-mock-1") {
  return {
    type: "a2ui",
    turnId,
    surfaceId: SURFACE_ID,
    messages: cannedSurface(),
  };
}
