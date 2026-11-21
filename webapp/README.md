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

Dark-first and dense. This is a working tool, not a consumer chat app:

- **The transcript is a log**, not a conversation. Every event is a row in one
  vertical stream with a mono gutter label and a subtle left rule. No bubbles.
- **Typography carries the weight.** Monospace for code, tool calls, paths and
  metrics; sans for prose. Base size in the transcript is 13px.
- **One accent**, in `src/index.css`, used only for state (running, focused,
  selected). Re-theming is editing one custom property.
- **State is never colour-only.** Status always ships an icon or a word too.
- Keyboard first: the composer always holds focus, `/` opens the palette, `Esc`
  cancels a turn, `Up` recalls the previous prompt.

## Layout

```
src/
  protocol.ts              wire types — the single source of truth, mirrors the doc
  index.css                design tokens (@theme) + transcript typography
  useQuartz.ts             the only place transport meets reducer
  lib/
    connection.ts          transport abstraction: connect/send/emit, reconnect backoff
    transcript.ts          pure reducer: protocol frames -> render rows (no React)
  components/
    Markdown.tsx           streaming-aware markdown + syntax highlighting
    HeaderBar.tsx          session, cwd, model, connection dot, New
    Transcript.tsx         role="log", follows the tail unless the user scrolled up
    StatusLine.tsx         status label / reconnect banner
    Composer.tsx           auto-grow textarea, steer-vs-prompt, image attach
    CommandPalette.tsx     fuzzy filter over session_state.commands
    rows/                  one component per row kind
    ui/                    shadcn-style primitives (Radix)
mock/
  script.mjs               the scripted session, pure frame builders
  server.mjs               ws server replaying it — `npm run mock`
  smoke.mjs                headless end-to-end check of the frame sequence
```

## Commands

```bash
npm install
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

## Testing without a browser

`npm run smoke` boots the mock on an ephemeral port, connects as a client, sends
`hello` and `prompt`, and asserts the guarantees from the protocol doc: one
`turn_start`, one `turn_end`, every `tool_result` after its `tool_call`, a failing
result present, an image result present, and all four `CommandResult` kinds.
No API key, no model, no Python.

## Dependencies worth justifying

| Package | Why |
| --- | --- |
| `react-markdown` + `remark-gfm` + `rehype-highlight` + `highlight.js` | Assistant text is markdown with fenced code. `react-markdown` renders to React elements instead of `dangerouslySetInnerHTML`, so model output is never injected as HTML. |
| `@radix-ui/react-*` | shadcn/ui primitives. Collapsible rows and the palette dialog need real focus management and ARIA, which is exactly what should not be hand-rolled. |
| `class-variance-authority`, `clsx`, `tailwind-merge` | The shadcn/ui baseline for variant styling. |
| `lucide-react` | Icons. Every state marker is an icon *plus* text. |
| `ws` (dev) | The mock server. |
| `vitest`, `@testing-library/*`, `jsdom` (dev) | The reducer and component tests. |
