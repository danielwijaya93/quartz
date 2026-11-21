# Quartz Web — Protocol Contract (v1, draft)

Status: **authoritative for Phase 1–3**. Implementations must follow this document;
if reality disagrees, change the document first in its own commit.

## Why this exists

`quartz` today has three transports over one core:

| Transport | Entry point | Core it drives |
| --- | --- | --- |
| TUI | `quartz` | `core/runtime.py` (`AgentRuntime`) + `core/turn.py` (`run_interactive_turn`) |
| Headless | `quartz -p` | same |
| ACP | `quartz --acp` | `core/loop.py` (`agent_loop`) directly |

`quartz web` is the **fourth** transport and the first one that is not a terminal.
It is *not* an ACP client. ACP exists so one client can drive *many* agents;
Quartz Web drives exactly one agent — Quartz — so translating through ACP would buy
genericity we never use and would cost fidelity on every event.

`quartz --acp` stays. It is the editor transport (Zed, etc.). Quartz Web is the
browser transport. Both sit on the same core.

## Command surface

```
quartz web [--host 127.0.0.1] [--port 8765] [--cwd PATH] [--open] [--dev]
```

- Binds **loopback only** by default. There is no auth: the server is a local
  single-user tool. Binding a non-loopback host must print a warning.
- `--open` opens the default browser once the server is listening.
- Served by uvicorn; the SPA is served from the same origin (no CORS, no proxy).
- Graceful shutdown closes every kernel.

## Wire protocol

One WebSocket at `/ws`. JSON text frames, one message per frame.
Every message is a flat object with a `type` string discriminator.

### Client → Server

| `type` | Fields | Meaning |
| --- | --- | --- |
| `hello` | `clientVersion: int` | First message. Server replies `session_state`. |
| `prompt` | `text: str`, `images?: [{mimeType, data}]`, `richUi?: bool` | Start a turn. Rejected with `error` if a turn is running. `richUi` asks this turn to also emit an `a2ui` surface; absent or `false` means none. |
| `steer` | `text: str` | Inject a steering message into the running turn. |
| `cancel` | — | Abort the running turn. |
| `command` | `name: str`, `arg?: str` | Run a slash command (see *Commands*). Name has no leading `/`. |
| `new_session` | — | Retire the current session, create a fresh one. |
| `load_session` | `sessionId: str` | Retire the current session, load `sessionId`. |

### Server → Client

Turn lifecycle:

| `type` | Fields |
| --- | --- |
| `session_state` | `sessionId`, `cwd`, `model`, `effort`, `messages: Message[]`, `turnRunning: bool`, `commands: Command[]` |
| `turn_start` | `turnId: str` |
| `status` | `label: str \| null` |
| `text_delta` | `text: str` |
| `thinking_delta` | `text: str` |
| `tool_call` | `id: str`, `code: str` |
| `tool_result` | `id: str`, `ok: bool`, `output: str`, `durationMs: int`, `images: [{mimeType, data}]`, `actions: object \| null` |
| `bash_notice` | `text: str` |
| `steering` | `text: str` |
| `compaction` | `summary: str`, `messagesBefore: int`, `messagesAfter: int`, `tokensBefore: int`, `tokensAfter: int` |
| `a2ui` | `turnId: str`, `surfaceId: str`, `messages: object[]` |
| `turn_end` | `status: "ok" \| "aborted" \| "interrupted" \| "error"`, `tokens: int`, `elapsedMs: int`, `error?: str` |
| `error` | `message: str`, `fatal: bool` |
| `command_result` | `name: str`, `ok: bool`, `result: CommandResult` |

`Message` is the same shape the session log already stores
(`{role, content, tool_calls?, tool_call_id?}`). The SPA renders history from it.

#### `a2ui` — a generated UI surface

`messages` is an **ordered** array of A2UI wire envelopes, protocol **v1.0**, and
the order is part of the payload: `createSurface` must precede the
`updateComponents` that fills it, or the surface is only half-built. The server
must not sort, merge or coalesce them; the client must not reorder them. Each
envelope is passed to the renderer untouched — the wire protocol does not model
A2UI's shapes, it only carries them.

It is a separate frame rather than a field on `text_delta` because a surface is
atomic: a consumer that drops or reorders one envelope, or that has to bolt
components onto whichever text row happens to be open, is left with a broken UI.
A dedicated frame keeps the sequence identifiable, lets a client that has no A2UI
renderer ignore it wholesale, and lets the reducer place the surface in the
transcript where the turn produced it.

```json
{ "type": "a2ui", "turnId": "t1", "surfaceId": "quartz-dashboard",
  "messages": [ { "version": "1.0", "createSurface": { "surfaceId": "quartz-dashboard" } },
                { "version": "1.0", "updateComponents": { "surfaceId": "quartz-dashboard",
                                                          "components": [] } } ] }
```

Whether an agent produces one, several, or no surfaces is the agent's business;
this frame is additive and changes no existing frame. A surface is *render* state,
not session state: it is not persisted, and a reconnect does not replay it.

### Ordering guarantees

1. `turn_start` precedes every other message of that turn.
2. Exactly one `turn_end` per `turn_start`.
3. `tool_result` for an id always follows its `tool_call`.
4. Deltas of a turn are never interleaved with another turn's deltas.
5. An `a2ui` frame belongs to one turn: it is emitted after that turn's
   `turn_start` and before its `turn_end`, and it never interleaves with another
   turn's frames. Its `turnId` names the turn it belongs to.
6. After `turn_end`, the session has been persisted to disk.

## Event mapping — the whole point

`core/events.py::TurnObserver` already defines every event a transport may render.
The terminal renders all seven; ACP renders two. **Quartz Web renders all seven.**
That parity is the deliverable; nothing here requires new core hooks.

| `TurnObserver` hook | Wire message |
| --- | --- |
| `on_status(label)` | `status` |
| `on_text_delta(text)` | `text_delta` |
| `on_thinking_delta(text)` | `thinking_delta` |
| `on_tool_call(id, code)` | `tool_call` |
| `on_tool_result(id, cell)` | `tool_result` |
| `on_compaction(result)` | `compaction` |
| `on_bash_notice(notice)` | `bash_notice` |
| `on_steering(text)` | `steering` |

Plus `TurnView`'s three turn-level hooks (`reset`, `finish`, `show_final`) which
bracket the turn and are not wire messages.

## Concurrency model

`run_interactive_turn` is **blocking**. The server must not run it on the event
loop.

- One turn at a time per session. A `prompt` while a turn is running is rejected
  with `error{fatal:false}`; it is not queued.
- The turn runs on a dedicated worker thread. Its observer pushes messages with
  `loop.call_soon_threadsafe(queue.put_nowait, msg)`.
- A single writer task drains the queue and sends frames. Observers never touch
  the WebSocket directly.
- `cancel` sets the turn's `abort_event` and interrupts the kernel — the same
  path `Esc` uses in the TUI.

## Commands

Phase 1 makes command handling transport-independent: handlers return data,
they do not print.

```python
@dataclass(frozen=True)
class CommandResult:
    kind: Literal["text", "list", "error", "needs_terminal"]
    title: str = ""
    body: str = ""                      # markdown, for kind="text"
    items: list[dict] = ()              # for kind="list"
    message: str = ""                   # for kind="error"/"needs_terminal"
```

Rules:

- `core/` owns the registry and the handlers' logic. **No `rich` import in `core/`.**
- `tui/commands.py` owns rendering: it turns a `CommandResult` into the exact
  `rich` output the terminal shows today. Terminal output must not change.
- Commands that fundamentally need a terminal modal (`/login`, `/model`,
  `/session`, `/continue`) return `kind="needs_terminal"` in Phase 1 rather than
  being reimplemented as web forms. Phase 4 turns them into real web controls.
- Commands that mutate `AgentRuntime` state (`/settings`, `/compact`, `/reset`)
  mutate the *live* runtime, so the next web turn sees the change immediately.

The advertised command list is sent in `session_state.commands` — the same
information ACP v2 would carry in `availableCommands`.

## Security posture (v1)

Local single-user tool. Explicit non-goals: multi-user, auth, remote exposure.

- Loopback bind by default; non-loopback prints a warning.
- The SPA is same-origin; no CORS headers are added.
- `cwd` is resolved from `--cwd`, defaulting to the process cwd. Session file
  access goes through the existing `Session` API; the web layer never accepts a
  raw filesystem path from the client.
- WebSocket rejects frames over 1 MiB and non-JSON payloads.

## Out of scope for v1

- ACP v2 upgrade (`configOptions`, `availableCommands`) — separate, later.
- Multi-session in one browser tab.
- A2UI *generation*. The wire carries a generated surface today (`a2ui`), but the
  server builds the first one from a fixed fixture; making a model choose the
  components is a separate change that does not alter this protocol.
- Auth, TLS, remote hosting.
