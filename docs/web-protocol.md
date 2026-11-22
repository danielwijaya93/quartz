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
| `hello` | `clientVersion: int`, `systemPromptAppend?: str`, `cwd?: str` | First message. Server replies `session_state`. Also sets the system-prompt append (see *System prompt append*). `cwd` (see *Choosing a working directory*) picks where the *initial* session runs; ignored once a runtime exists. Re-sending it is how a client changes the append mid-session. |
| `prompt` | `text: str`, `images?: [{mimeType, data}]` | Start a turn. Rejected with `error` if a turn is running. |
| `steer` | `text: str` | Inject a steering message into the running turn. |
| `cancel` | — | Abort the running turn. |
| `command` | `name: str`, `arg?: str` | Run a slash command (see *Commands*). Name has no leading `/`. |
| `new_session` | `cwd?: str` | Retire the current session, create a fresh one, in `cwd` when given (see *Choosing a working directory*). |
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
| `turn_end` | `status: "ok" \| "aborted" \| "interrupted" \| "error"`, `tokens: int`, `elapsedMs: int`, `error?: str` |
| `error` | `message: str`, `fatal: bool` |
| `command_result` | `name: str`, `ok: bool`, `result: CommandResult` |

`Message` is the same shape the session log already stores
(`{role, content, tool_calls?, tool_call_id?}`). The SPA renders history from it.

### System prompt append

`hello.systemPromptAppend` is a client-supplied string the server appends to the
Quartz system prompt for this session. It is the web twin of the ACP vendor
extension (`_meta.quartz.systemPrompt.append`) and exists for the same reason: a
client that owns a rendering surface also owns the instructions that make the
model produce output for it.

Rules:

1. **The client owns the text.** Nothing in `src/quartz/` contains the OpenUI
   Lang prompt; the browser builds it from the component-library spec it ships.
2. **Per session, and idempotent.** The prompt is applied to the live runtime on
   every `hello`, always as `base + "\n\n" + append`. Setting it twice cannot
   stack two copies, and `""` withdraws it, so toggling Rich UI off puts the next
   turn back on the stock system prompt -- no session rebuild, no stale prompt.
3. **Optional and forgiving.** Absent, empty or non-string means "no append": the
   session runs on the stock prompt. A bad value never produces an `error` frame.
4. **It survives a session swap.** `new_session` / `load_session` re-apply the
   current append to the new runtime, because it is a client preference rather
   than a property of one session.
5. **It does not change the wire between `session_state` messages.** Only the
   system prompt changes, so no frames are added or reordered.

`quartz web`'s client sends it when Rich UI is on (`Markdown | Rich UI` in the
header) and sends `""` when it is off.

### Choosing a working directory

A folder-first sidebar means the browser, not the command line, decides where a new
session runs: `quartz web --cwd` is only the starting point.

Rules:

1. `new_session.cwd` and `hello.cwd` are absolute paths or `~`-relative, resolved
   on the server. A relative path is rejected like any other unusable value.
2. `new_session.cwd` builds the new runtime *in that directory* — the kernel's
   cwd, the system prompt and the session's `meta.cwd` all follow. No restart.
3. `hello.cwd` applies to the initial session only. Once a runtime exists, `hello`
   is an idempotent config frame and its `cwd` is ignored; use `new_session`.
4. `load_session` takes no `cwd`: a resumed session already knows its own directory
   from its `meta` record, and letting a client override it would silently replay
   history into a kernel rooted somewhere else. The server builds the runtime in that
   folder — kernel cwd, system prompt and `session_state.cwd` all follow it. If the
   folder has since been deleted, the session opens in the server's own directory
   instead (not an error), and `session_state.cwd` says where it landed.
5. Optional and forgiving. An absent, empty, non-string or non-existent value falls
   back to the server's `--cwd`; it never produces a fatal `error` frame, because a
   page that cannot start a session is worse than one that starts in the wrong
   folder and says so in `session_state.cwd`.

### Ordering guarantees

1. `turn_start` precedes every other message of that turn.
2. Exactly one `turn_end` per `turn_start`.
3. `tool_result` for an id always follows its `tool_call`.
4. Deltas of a turn are never interleaved with another turn's deltas.
5. After `turn_end`, the session has been persisted to disk.

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
  `/continue`) return `kind="needs_terminal"` in Phase 1 rather than being
  reimplemented as web forms. Phase 4 turns them into real web controls. `/session`
  is registered `needs_terminal` for the same reason (the terminal has the richer
  form), but it answers in the browser too: the session id, log path and snapshot
  path as ordinary text.
- Commands that mutate `AgentRuntime` state (`/settings`, `/compact`, `/reset`)
  mutate the *live* runtime, so the next web turn sees the change immediately.

The advertised command list is sent in `session_state.commands` — the same
information ACP v2 would carry in `availableCommands`. Each entry is
`{name, description, usage, needsTerminal}`, with the name carrying no leading `/`.
`needsTerminal` is derived from the registry, never maintained separately, so a
client can badge (or hide) a command that cannot work in a browser without guessing:
`/session` is `false` there because it answers with real text.

## Sidebar and filesystem API

The sidebar is *folder-first*: a workspace (a `cwd`) holds its sessions, and a
person picks that folder in the page. That requires two things the phase-1
protocol did not have: server-side sidebar state, and a directory listing the
picker can walk.

### Sidebar state

`~/.quartz/web/sidebar.json`, owned by the web transport alone (the terminal
never reads it). Missing or corrupt files fall back to empty state:

```json
{
  "version": 1,
  "hiddenSessions": ["20261121-..."],
  "importedSessions": ["20261118-..."],
  "renames": {"20261121-...": "Sidebar work"},
  "workspaces": [{"cwd": "/home/x/project", "label": "project"}]
}
```

**Visibility rule** — one sentence, implemented by the server and relied on by the
client:

> A session appears in the sidebar when its `origin` is `"web"` **or** it is in
> `importedSessions`, **and** it is not in `hiddenSessions`.

Consequences worth stating, because they are the reason this lives in the
transport rather than in `core/`:

- The sidebar shows sessions started in the browser, plus sessions the user
  *explicitly* imported. Importing a terminal session is never automatic.
- **Archiving is not deleting.** `hiddenSessions` only stops the sidebar from
  listing a session: no file is removed, rewritten or moved, and
  `~/.quartz/sessions/*.jsonl` is untouched. `POST /api/sessions/restore` undoes
  it, which is what makes the archive dialog ("Not in sidebar") the other half of
  the same feature.
- `workspaces` holds folders added by hand, so an empty folder can be opened and
  used before it has a session. A folder that has sessions appears without an
  entry here; the folder comes from the sessions themselves.
- Renaming writes `renames`, never the session log. `origin` lives in the session's
  own `meta` record (`"web"` or `"cli"`, missing means `"cli"`).

### HTTP endpoints

All of these are same-origin, unauthenticated, loopback-first — the same posture
as the WebSocket. Blocking filesystem work runs off the event loop.

| Method + path | Body | Returns |
| --- | --- | --- |
| `GET /api/sessions` | — | `{sessions, sidebar}`: every known session across all `cwd`s with `sessionId`, `cwd`, `title`, `updatedAt`, `origin`, `cwdExists`; plus the whole sidebar state. One call draws the whole sidebar. |
| `POST /api/sessions/hide` | `{sessionId}` | The same `{sessions, sidebar}` shape, after hiding it. |
| `POST /api/sessions/restore` | `{sessionId}` | Same, after un-hiding it. |
| `POST /api/sessions/import` | `{sessionId}` | Same, after importing it. |
| `POST /api/sessions/rename` | `{sessionId, title}` | Same, after storing the rename. An empty `title` clears it. |
| `POST /api/workspaces` | `{cwd}` | Same, after adding `cwd` to `workspaces`. |
| `GET /api/fs/list?path=` | — | `{path, parents, entries, error}` (see below). |
| `POST /api/fs/mkdir` | `{path, name}` | The listing for `path` after creating `name` inside it. |

### Why a path API is acceptable here

This document used to promise that *"the web layer never accepts a raw filesystem
path from the client"*. A folder-first sidebar cannot keep that promise: the whole
point is choosing a directory from the page. So the promise is replaced with a
smaller, checkable one.

`GET /api/fs/list` takes an absolute path (or `~`) and returns:

```json
{"path": "/home/x", "parents": [{"name": "home", "path": "/home"}],
 "entries": [{"name": "project", "path": "/home/x/project", "isDir": true}], "error": null}
```

Each entry carries its own full `path` so the client never joins a name onto a
parent: a client that assembled paths from strings would be one escaping bug away
from opening a different directory than the row it drew.

What bounds it:

1. **Directories only.** The listing returns directory entries, never file
   contents, sizes, mtimes or symlink targets. Files are not readable through it.
2. **Nothing mutates except `mkdir`.** `POST /api/fs/mkdir` creates exactly one
   directory inside `path`; `.`, `..` and anything containing a path separator are
   rejected. There is no delete, no rename, no move, no write, no chmod.
3. **A relative path is an error.** Only absolute and `~` paths resolve, so the
   API cannot be used to wander relative to some other process's cwd.
4. **A bad path is a value, not a crash.** An unreadable or missing path returns
   `error` with an empty listing; the picker shows it and lets the user go back.
5. **Same trust boundary as everything else.** The server already runs a Python
   kernel as this user on request from this socket: an `ls` of a directory is not
   a new capability. Nothing here is exposed beyond the bind address, which is
   loopback by default and unauthenticated by design — so exposing it further is
   the operation that needs care, not listing.

If Quartz Web ever grows auth or a multi-user mode, this section is the first
thing to revisit.

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
- Multi-session in one browser tab (the sidebar switches sessions; it does not
  run two at once).
- Live session status (running / needs input) in the sidebar.
- Rich-UI surfaces as a wire concept. The client renders OpenUI Lang itself
  (`webapp/src/components/OpenUISurface.tsx`); the only protocol support it needs
  is `hello.systemPromptAppend`.
- Auth, TLS, remote hosting.