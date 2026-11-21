# OpenUI rendering — visual proof

Captured from the built SPA (`npm run build`, served by `quartz web`) at
1200×1150, 2× device scale, in a real Chrome via Playwright.

| File | What it shows |
| --- | --- |
| `real-light.png` | **Live model**, Rich UI on, light theme: a real OpenUI surface (2 KPI cards, a bar chart, a 3-column table, and a Select + Submit form) rendered from a real `deepseek-flash` reply. No raw openui-lang on screen. |
| `real-dark.png` | The same surface after switching to dark. Both the page *and* the OpenUI palette flip (`data-openui-theme="dark"`); chart axes, table rules and card borders all invert. |
| `real-form-submit.png` | The form **selected and submitted**: the Select shows "High — multi-step reasoning", the turn footer reads `Finished · 15,718 tokens`, and the next turn is already starting — the `@ToAssistant(...)` submit went back into the same session as a user message. |
| `real-markdown.png` | Rich UI **toggled off** on the same conversation. The same replies now render as plain markdown — honestly showing the openui-lang source, because those turns were produced while the model *was* told to emit it. |
| `mock-light.png`, `mock-dark.png` | The same check against `npm run mock`, which replays a canned openui-lang turn when (and only when) the client's `hello` carried a system-prompt append. This is the no-API-key proof, and it exercises a **partial first render**: the program streams in two chunks, so the surface appears before it is complete. |

Reproduce:

```bash
cd webapp && npm ci && npm run build
cd .. && uv run quartz web --port 8791        # then open http://127.0.0.1:8791
```

The toggle is `Markdown | Rich UI` in the header; the choice survives a reload
(`localStorage["quartz-rich-ui"]`).
