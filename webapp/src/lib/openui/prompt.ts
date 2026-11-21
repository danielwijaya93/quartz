import {
  generateSystemPrompt,
  type LibrarySpec,
  type SystemPromptOptions,
} from "@openuidev/lang-core";
import spec from "@/generated/spec.json";

/**
 * The OpenUI Lang system prompt, built once per page load from the compiled spec.
 *
 * The spec is a build artefact of `src/library.ts` (`npm run generate`), and it is
 * imported rather than read from disk: this is a static SPA, so the spec is
 * inlined at build time and the prompt is assembled in the browser. That is the
 * whole reason the prompt is client-supplied -- the component library, and the
 * spec that describes it, live here, and the server never has to own a copy.
 */
let cachedPrompt: string | null = null;

export function getOpenUISystemPrompt(): string {
  if (cachedPrompt !== null) return cachedPrompt;

  // The generator writes `promptOptions` next to the library so a single file
  // carries both halves; `generateSystemPrompt` takes them as separate arguments.
  const { promptOptions, ...library } = spec as LibrarySpec & {
    promptOptions?: SystemPromptOptions;
  };

  cachedPrompt = generateSystemPrompt({ library, promptOptions });
  return cachedPrompt;
}

/** Test seam: forget the cached prompt (a spec change must be observable). */
export function resetOpenUISystemPromptCache(): void {
  cachedPrompt = null;
}
