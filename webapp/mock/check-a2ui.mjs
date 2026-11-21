/**
 * Validate the canned surface against the real A2UI processor, with no browser.
 *
 * This is the cheap half of the paint check: it proves the envelopes pass the
 * renderer's own validation (component names, ids, ordering) before anything is
 * screenshotted. Run with `npm run check:a2ui`.
 */
import { MessageProcessor } from "@a2ui/web_core/v1_0";
import { basicCatalog } from "@a2ui/web_core/catalogs/basic/v1";
import { cannedSurface, SURFACE_ID } from "./a2ui-fixture.mjs";

const processor = new MessageProcessor([basicCatalog], undefined, { version: "1.0" });
const errors = processor.processMessagesWithErrors(cannedSurface());
const surfaces = [...processor.getSurfaces().values()];

if (errors.length > 0) {
  console.error("A2UI rejected part of the fixture:");
  for (const error of errors) console.error(` - ${JSON.stringify(error.error)}`);
  process.exit(1);
}
if (surfaces.length !== 1) {
  console.error(`expected exactly one surface, got ${surfaces.length}`);
  process.exit(1);
}
const [surface] = surfaces;
if (surface?.id !== SURFACE_ID) {
  console.error(`expected surface ${SURFACE_ID}, got ${surface?.id}`);
  process.exit(1);
}
if (!surface.componentsModel.get(surface.rootId)) {
  console.error(`surface has no root component ('${surface.rootId}')`);
  process.exit(1);
}

const componentIds = cannedSurface()[1].updateComponents.components.map((entry) => entry.id);

console.log(
  `A2UI OK — surface '${surface.id}', root '${surface.rootId}', ${componentIds.length} components ` +
    `(${componentIds.join(", ")}), catalog '${basicCatalog.id}', protocol ${basicCatalog.protocolVersion}`,
);
