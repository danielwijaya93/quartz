# Quartz Web (client)

The browser client for `quartz web`. Static Vite + React 19 + TypeScript output,
served by the Python server from the same origin. There is no Node process at
runtime: `dist/` is just files.

The wire contract lives in [`../docs/web-protocol.md`](../docs/web-protocol.md).
This package implements the **client half** of it; the Python server is a separate
track.

## Why a web transport at all

A terminal can only render text. Quartz Web renders the same seven
`TurnObserver` events the TUI renders — text, thinking, tool calls, tool results,
compaction boundaries, background-job notices and steering — but with real
presentation: syntax-highlighted code, inline images, collapsible cells,
measurable metrics.

## Design

Built on **HeroUI v3** with first-class light and dark themes. The library is the
foundation; the transcript is the part designed for this app.

- **Both themes are first-class.** Light, dark, and system, in one control. The
  theme is applied before first paint, so there is no flash. Tokens live in
  `src/index.css` and `@heroui/styles`; no component carries a raw colour.
- **Comfortable, not cramped.** Prose is 15px on a 768px reading column. The
  transcript is a sequence of turns and cells, not a log tail.
- **The transcript is a structure, not an announcement.** It carries `role="log"`
  but is *not* a live region: a separate announcer speaks coarse events only
  (turn started, tool finished, turn finished, error).
- **State is never colour-only.** Every status carries an icon or a word too.
- **Keyboard first:** the composer always holds focus, `/` opens the palette, `Esc`
  cancels a turn or closes the palette and returns focus to the composer, `Up`
  recalls the previous prompt.

## Rich UI (OpenUI generative UI)

Assistant replies can be drawn as a live **OpenUI surface** — charts, tables, KPI
cards, forms — instead of prose. `Markdown | Rich UI` in the header switches it,
and the choice is remembered.

This is a *preference with a consequence*, not a view filter. With Rich UI on the
client appends the OpenUI Lang system prompt to the session
(`hello.systemPromptAppend`, the web twin of ACP's
`_meta.quartz.systemPrompt.append`); with it off the append is withdrawn, so the
model is never told to emit openui-lang and the prompt is not paid for. Rendering
and prompting therefore read the same value, and a fenced or partial reply is
stripped/handled rather than shown as DSL.

| Piece | Where |
| --- | --- |
| Component library + prompt options | `src/library.ts` (re-exports `openuiLibrary`, adds Quartz's preamble/rules) |
| Compiled spec | `src/generated/spec.json`, from `npm run generate` (`openui generate src/library.ts --spec`) |
| System prompt | `src/lib/openui/prompt.ts` (`generateSystemPrompt` over the spec), imported only when Rich UI is on |
| Fence/detection helpers | `src/lib/openui/lang-helpers.ts` |
| The renderer | `src/components/OpenUISurface.tsx` — the only file that imports `@openuidev/*` |
| Where the decision is made | `src/components/rows/AssistantRowView.tsx` |

The renderer is a lazy chunk, and its 356 kB stylesheet is imported *by that
module* rather than by `index.css`, so a Markdown-only reader downloads neither.
A `@ToAssistant(...)` submit becomes the next user prompt through the ordinary
`prompt` frame, so the model continues the same session; `OpenUrl` opens a link.
Form state is persisted onto the row (`setRowFormState`), so toggling, resizing or
adding a row cannot reset a half-filled form.

`spec.json` is **committed**: the SPA is static, `prebuild` regenerates it so a
drift is caught by `git diff` in review, and a checkout can build without the
`openui` CLI. Re-run `npm run generate` whenever `src/library.ts` changes.

### Developing against the mock

The mock replays an openui-lang turn instead of the markdown script when the
client's `hello` carried a `systemPromptAppend` — exactly the condition under
which the real model is told to emit openui-lang. So the toggle exercises the
whole path (prompting decision, renderer, form round-trip) with no API key.

## Layout

```
src/
  protocol.ts              wire types — the single source of truth, mirrors the doc
  index.css                HeroUI import + theme tokens + transcript typography
  useQuartz.ts             the only place transport meets reducer
  lib/
    connection.ts          transport abstraction: connect/send/emit, reconnect backoff
    transcript.ts          pure reducer: protocol frames -> render rows (no React)
    theme.ts               light/dark/system: the one theme contract, OpenUI theme mirror
    richUi.ts              the Markdown/Rich UI preference: storage, three-state-safe
    useRichUi.ts           the preference as React state + the lazily-imported prompt
    openui/                prompt generation and openui-lang detection
    announcements.ts       rows -> the coarse events a screen reader should hear
    commandText.ts         columnar `kind: "text"` bodies (e.g. /help) -> lists
  components/
    Markdown.tsx           streaming-aware markdown + theme-aware highlighting
    HeaderBar.tsx          session chips, connection chip, Rich UI toggle, theme, New
    RichUiToggle.tsx       Markdown / Rich UI (icon + text, never colour only)
    ThemeToggle.tsx        light / dark / system
    OpenUISurface.tsx      the OpenUI renderer, its library and action wiring
    Transcript.tsx         role="log", follows the tail unless the user scrolled up
    TranscriptAnnouncer.tsx  the only aria-live node in the transcript area
    StatusLine.tsx         spinner + status label / reconnect banner
    Composer.tsx           auto-grow textarea, steer-vs-prompt, image attach
    CommandPalette.tsx     Modal + fuzzy filter over session_state.commands
    rows/                  one component per row kind
mock/
  script.mjs               the scripted session, pure frame builders
  server.mjs               ws server replaying it — `npm run mock`
  smoke.mjs                headless end-to-end check of the frame sequence
```

## Commands

```bash
npm install
npm run generate  # compile src/library.ts -> src/generated/spec.json
npm run dev       # Vite dev server; proxies /ws to the mock
npm run mock      # mock server on ws://127.0.0.1:8765
npm run smoke     # drive the mock as a client, assert protocol ordering
npm run test      # vitest (reducer + components)
npm run typecheck # tsc --noEmit (strict)
npm run build     # static bundle -> dist/
npm run verify    # typecheck + test + smoke + build
```

### Developing against the mock

```bash
# terminal 1
npm run mock

# terminal 2
npm run dev        # http://localhost:5173
```

The dev server proxies `/ws` to `127.0.0.1:8765`, so the app uses the same
same-origin URL it will use in production. If the mock is on another port, point
the app at it directly:

```bash
VITE_WS_URL=ws://127.0.0.1:8799 npm run dev
```

## Theming

`src/lib/theme.ts` is the one place the three-state theme lives. It wraps HeroUI's
`useTheme`, which persists the user's *intent* (`light` / `dark` / `system`), watches
`prefers-color-scheme` live while in system mode, and applies both the `.dark`/`.light`
class and `data-theme` on `<html>` in a layout effect.

The no-flash guarantee is a second, deliberate duplication: a small inline script
in `index.html` reads the same `localStorage` key and applies the same class before
the first paint. `src/lib/theme.test.ts` runs that script out of the HTML to prove
it agrees with the hook.

## Testing without a browser

`npm run smoke` boots the mock on an ephemeral port, connects as a client, sends
`hello` and `prompt`, and asserts the guarantees from the protocol doc: one
`turn_start`, one `turn_end`, every `tool_result` after its `tool_call`, a failing
result present, an image result present, and all four `CommandResult` kinds.
No API key, no model, no Python.

## Dependencies worth justifying

| Package | Why |
| --- | --- |
| `@heroui/react` v3 + `@heroui/styles` | The design system. Components, tokens, and both themes. No provider needed in v3. |
| `react-markdown` + `remark-gfm` + `rehype-highlight` + `highlight.js` | Assistant text is markdown with fenced code. `react-markdown` renders to React elements instead of `dangerouslySetInnerHTML`, so model output is never injected as HTML. `highlight.js` is only used through `rehype-highlight`; its colours are two token palettes in `index.css`, not a bundled theme file. |
| `lucide-react` | Icons outside HeroUI's own set — HeroUI ships only info/warning/danger/success/chevron glyphs, and this app needs sun, moon, monitor, arrow, stop, plus, brain, terminal, image and copy. |
| `@openuidev/react-ui` 0.18.10, `@openuidev/react-lang` 1.1.1, `@openuidev/lang-core` 1.1.1, `@openuidev/react-headless` 0.18.10 | The OpenUI generative-UI component library, parser and prompt generator. Pinned exactly, not ranged: the spec in `src/generated/spec.json` is compiled from a specific library version, and a minor bump can change component signatures under it. `react-headless` is a peer of `react-ui` (and is what supplies the store the renderer reads). |
| `zustand` 4.5.7 | Required peer of `@openuidev/react-ui` (`^4.5.5`). Pinned to the 4.x line the library declares, not the 5.x that is current. |
| `@openuidev/cli` 0.6.3 (dev) | Generates `src/generated/spec.json` from `src/library.ts`. Build-time only. |
| `ws` (dev) | The mock server. |
| `vitest`, `@testing-library/*`, `jsdom` (dev) | The reducer and component tests. |