/**
 * The one place A2UI's processor and catalog are wired up.
 *
 * Everything A2UI-specific that the app has to know lives behind this module: the
 * catalog list, the processor options, error mapping, and the generated surface
 * list. `A2uiRowView` renders what this returns and knows nothing else.
 *
 * That is deliberate. The Quartz catalog (the follow-up) is a change *here* --
 * `[basicCatalog, quartzCatalog]` -- and a matching change on the agent side; no
 * component needs to learn about it. Same for the protocol: the surface set is
 * derived from whatever envelopes the server sent.
 *
 * Versions, pinned in `package.json`:
 *   @a2ui/react    0.15.0  (React renderer; peers React 18/19 + zod 3)
 *   @a2ui/web_core 0.15.0  (protocol v1.0 processor + basic catalog)
 */

import { MessageProcessor } from "@a2ui/web_core";
import type { Catalog, SurfaceModel } from "@a2ui/web_core";
import type { ReactCatalogComponent } from "@a2ui/react";
import { basicCatalog, injectBasicCatalogStyles } from "@a2ui/web_core/catalogs/basic/v1";

/**
 * Catalogs this renderer can draw, in priority order.
 *
 * One cast, in one place: the React renderer types a catalog entry as "React
 * implementation or web component", while a catalog built from generated web
 * components is typed more narrowly. The runtime objects are identical -- the
 * renderer registers exactly these web components -- so the cast is about the
 * generic parameter, not about a lie at runtime.
 */
const CATALOGS = [basicCatalog] as unknown as Catalog<ReactCatalogComponent>[];

/** One problem the renderer reported. Already user-facing enough to print. */
export interface A2uiIssue {
  message: string;
  path?: string;
}

/** What a frame turned into: surfaces to draw, or reasons it could not. */
export interface A2uiSurfaceSet {
  surfaces: SurfaceModel<ReactCatalogComponent>[];
  issues: A2uiIssue[];
  /** Release the processor's subscriptions. Call exactly once, on unmount. */
  dispose: () => void;
}

let stylesInjected = false;

function injectStylesOnce(): void {
  if (stylesInjected) return;
  stylesInjected = true;
  try {
    // The basic catalog's own defaults: spacing, font scale, border width. Our
    // colour overrides live in `index.css` on `.a2ui-scope`, which wins on
    // specificity, so one stylesheet stays the single source of truth for the
    // palette.
    injectBasicCatalogStyles();
  } catch {
    // jsdom, or a browser without constructable stylesheets: the components
    // still render, they just keep their inline fallbacks.
  }
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * Feed one frame's envelopes to a fresh processor.
 *
 * A processor per frame is the idempotent reading of "one surface per frame":
 * replaying the same array cannot create a second surface, because the second
 * processor starts empty and builds the same one. `processMessagesWithErrors`
 * is used instead of `processMessages` so a rejected envelope is *reported*
 * rather than thrown out of a render.
 */
export function createSurfaceSet(messages: readonly unknown[]): A2uiSurfaceSet {
  injectStylesOnce();
  const processor = new MessageProcessor<ReactCatalogComponent>(CATALOGS, undefined, {
    version: "1.0",
  });
  const issues: A2uiIssue[] = [];

  if (messages.length === 0) {
    issues.push({ message: "The server sent no A2UI messages for this surface." });
  } else {
    try {
      for (const error of processor.processMessagesWithErrors(messages)) {
        const body = error.error;
        const path = "path" in body && typeof body.path === "string" ? body.path : undefined;
        issues.push({
          message: typeof body.message === "string" ? body.message : "A message was rejected.",
          ...(path === undefined ? {} : { path }),
        });
      }
    } catch (error) {
      issues.push({ message: describeError(error) });
    }
  }

  return {
    surfaces: [...processor.getSurfaces().values()],
    issues,
    dispose: () => processor.dispose(),
  };
}
