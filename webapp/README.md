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

## Layout

```
src/
  protocol.ts              wire types — the single source of truth, mirrors the doc
  index.css                HeroUI import + theme tokens + transcript typography
  useQuartz.ts             the only place transport meets reducer
  lib/
    connection.ts          transport abstraction: connect/send/emit, reconnect backoff
    transcript.ts          pure reducer: protocol frames -> render rows (no React)
    theme.ts               light/dark/system: the one theme contract
    richUi.ts              markdown vs dashboard response format (persisted, per prompt)
    a2ui/processor.ts      the only place A2UI's processor + catalog are wired up
    a2ui/useA2uiSurfaceSet.ts  one processor per frame, disposed on unmount
    announcements.ts       rows -> the coarse events a screen reader should hear
    commandText.ts         columnar `kind: "text"` bodies (e.g. /help) -> lists
  components/
    Markdown.tsx           streaming-aware markdown + theme-aware highlighting
    HeaderBar.tsx          session chips, connection chip, theme toggle, New
    ThemeToggle.tsx        light / dark / system
    RichUiToggle.tsx       markdown / dashboard response format
    Transcript.tsx         role="log", follows the tail unless the user scrolled up
    TranscriptAnnouncer.tsx  the only aria-live node in the transcript area
    StatusLine.tsx         spinner + status label / reconnect banner
    Composer.tsx           auto-grow textarea, steer-vs-prompt, image attach
    CommandPalette.tsx     Modal + fuzzy filter over session_state.commands
    rows/                  one component per row kind
                           rows/A2uiRowView.tsx draws an `a2ui` surface (lazy chunk)
mock/
  script.mjs               the scripted session, pure frame builders
  a2ui-fixture.mjs         the canned A2UI v1.0 surface, mirroring the server's
  check-a2ui.mjs           validate the fixture against the real renderer, no browser
  server.mjs               ws server replaying it — `npm run mock`
  smoke.mjs                headless end-to-end check of the frame sequence
```

## A2UI surfaces

A surface arrives as one `a2ui` frame: an ordered list of A2UI v1.0 wire
envelopes (`createSurface`, then `updateComponents`). `lib/a2ui/processor.ts` feeds
them to a `MessageProcessor` and `rows/A2uiRowView.tsx` draws each resulting
surface with `A2uiSurface` from `@a2ui/react`. Rendering is a lazy chunk, so a
user who stays in Markdown mode never downloads the renderer.

The frame is emitted only when the client asks: the header's **Markdown /
Dashboard** control sets `richUi: true` on the next `prompt` frame and persists
the choice in `localStorage`.

Two things it deliberately does not do yet:

- **The surface is canned.** `src/quartz/web/a2ui_fixture.py` returns a fixed
  surface; no model chose its components. This is a renderer check, not
  generation.
- **The catalog is Google's basic catalog**, so the surface looks like generic
  A2UI widgets, not like Quartz. A Quartz catalog implemented with HeroUI
  components is the next step, and it plugs in at `lib/a2ui/processor.ts` and in
  `src/quartz/web/` on the agent side — nowhere else.

Theming is one block of CSS variables in `src/index.css`: `--a2ui-color-*` mapped
onto HeroUI tokens inside `.a2ui-scope`, so both themes come from the design
system and no component carries a raw colour.

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
node mock/check-a2ui.mjs   # the fixture's envelopes vs. the real A2UI processor
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
| `ws` (dev) | The mock server. |
| `vitest`, `@testing-library/*`, `jsdom` (dev) | The reducer and component tests. |
