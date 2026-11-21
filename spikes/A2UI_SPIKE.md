# A2UI spike: verdict

**GO — with caveats.** `a2ui-agent-sdk` drives Quartz's provider layer cleanly, produces
valid generative UI from a real model, and rejects bad output with an error good enough to
hand back to the model. Nothing in Quartz's provider layer has to change.

Date: 2026-11-21 · Branch: `feat/a2ui-spike` · Not merged; this is a decision document.

---

## What was proven

| Claim | Result |
| --- | --- |
| Adapter from Quartz's provider to A2UI's compiler | **Works.** ~15 lines, no provider changes |
| Real model produces valid A2UI UI | **Works.** `deepseek-flash`, 3-card KPI layout, 0 errors |
| Server-side validation catches a bad message | **Works.** Names the component *and* the catalog |
| Error string is usable for self-correction | **Yes** — see caveat 2 on *how* to feed it back |
| Renders in a browser | **Not tested here.** No renderer was wired up (out of scope) |

## Exact versions that work

```
Python:  a2ui-agent-sdk 0.12.0
         a2ui-core      0.7.0
         antlr4-python3-runtime 4.13.2, babel 2.18.0, regex 2026.11.19
Protocol: A2UI v1.0
JS peer:  @a2ui/web_core 0.15.0, @a2ui/react 0.15.0 (both speak protocol v1.0)
```

**Marginal install cost is 5 packages, confirmed**, not the 14 the earlier estimate claimed
(that number came from an empty venv; `pydantic`, `jsonschema`, `pyyaml`, `referencing` and
`attrs` were already present via FastAPI).

## The adapter

`spikes/a2ui_spike.py::quartz_text_chunks` — 15 lines. Quartz streams typed events
(`TextDelta`, `ThinkingDelta`, `ToolCallDelta`, `UsageEvent`); the Express compiler wants
text, so thinking and tool-call events are filtered out. It holds the `StreamHandle` open and
closes it in a `finally`, because the consumer can stop early on a compile error.

```python
handle = client.stream(model, messages, None, effort=effort)
for event in handle:
    if isinstance(event, TextDelta):
        yield event.text
```

**What Quartz's provider layer must change: nothing.** `build_provider()` and
`LLMProvider.stream()` already expose exactly what is needed.

## Does validation actually work?

Yes, and it is the reason to take the SDK rather than parse JSON in the browser:

```
Unknown component 'NotARealComponent' not defined in catalog
'https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json'.
- Help: Please correct the syntax error in your Express DSL.
```

Two modes, and the default is the **loud** one:

- `open_stream()` (default, `after_rejected_block="end"`) — **raises** `A2uiError` out of
  `feed()`. The error carries `partial_results`.
- `open_stream(after_rejected_block="continue")` — returns an `ErrorPart` and keeps reading.
  **This is the mode a retry loop wants**, and it is not the default.

## Three gotchas that are not in the docs

1. **The `<a2ui>` sentinel is mandatory and its absence is silent.** Without
   `<a2ui>…</a2ui>`, the block is passed through as `TextPart` prose and compiles to
   *nothing*. No error, no warning. This is the single most likely way an integration
   appears broken for no reason.
2. **`progressive_keys` defaults to `{'text'}`, which explodes wire volume.** It re-sends a
   component on every text delta. Measured: a 2-component UI → **3 envelopes**; a 3-card
   layout → **37**. `progressive_keys=frozenset()` coalesces to **1 envelope per surface**.
   The web transport must choose deliberately. Progressive is nice for perceived latency and
   wasteful on the wire; coalesced is the sane default for a local single-user tool.
3. **`A2uiError` lives in `a2ui.parser.errors`**, not `a2ui.errors`.

Also: `create_processor()` requires `A2uiRendererCapabilities` with a non-empty
`supported_catalog_ids`. An empty list raises. The *renderer* declares which catalogs it can
draw; the agent then emits only from those. This is the correct design, and it is the
handshake where a real integration should be careful.

## The prompt cost

`processor.prompt_snippet` is **23,054 characters** generated from the catalog schema. It
replaces a hand-written prompt (good — it cannot drift from the catalog), but it is prepended
to every A2UI-capable turn. Against Quartz's existing system prompt this is a real token
budget line item and should be cached at the provider level where possible.

## What `ModelStreamFn` would need from Quartz

The SDK documents `ModelStreamFn = Callable[[session_id, system_prompt, user_message],
AsyncIterable[str]]`. Quartz has all three, with two shape differences:

- **Sync vs async.** `LLMProvider.stream()` returns a blocking iterator. An async adapter must
  bridge with a worker thread — exactly what `quartz/web/session.py` already does for turns.
  Do not `next()` a blocking stream inside the event loop.
- **System prompt override per request.** `agent_loop` takes one `system_prompt` for the whole
  session, and `build_system_prompt()` composes it once at bootstrap. An A2UI turn needs
  *that prompt plus the 23k snippet*, for that request only. No core change is strictly
  required — pass the composed prompt into the A2UI path directly rather than through
  `agent_loop` — but it means the A2UI path does not go through `agent_loop`, so it also does
  not get tool calls, kernel execution, compaction, or steering. **That is the real design
  decision this spike surfaces** (see below).

## Where A2UI should ride on the existing web wire protocol

**Recommendation: a new frame type, `a2ui`, carrying an ordered `messages` array.**

Not a field on `text_delta`, for a specific reason: `docs/web-protocol.md` guarantees
"exactly one `turn_end` per `turn_start`" and "deltas of a turn are never interleaved with
another turn's deltas". A `createSurface` is only meaningful as part of an ordered sequence —
`createSurface` then `updateComponents` then `updateDataModel` — and a consumer that drops or
reorders one is left with a half-built surface. A dedicated frame keeps that sequence atomic
and identifiable, and it lets the SPA's reducer place UI in the transcript at the point in the
turn where it was produced, rather than bolting it onto whichever text row is currently open.

Proposed shape (additive; no existing frame changes):

```json
{ "type": "a2ui", "turnId": "t1", "surfaceId": "sales-kpis",
  "messages": [ { "version": "1.0", "createSurface": { … } } ] }
```

The frame should be emitted *after* `turn_end` for coalesced mode (one frame, whole surface),
or during the turn in progressive mode. It must not break guarantee 5 (session persisted before
`session_end`) — A2UI state is not session state, it is render state, so it does not need
persisting.

## Top 3 risks for the real integration

1. **A2UI cannot ride inside `agent_loop`.** The UI-producing call is a *separate* model call
   with a different system prompt and no tools. So an A2UI turn cannot also run kernel cells.
   Either UI is a distinct turn mode the user asks for (simplest, honest), or we build a
   two-phase turn (tool work, then a UI summary call) — more powerful, more moving parts, and
   double the token cost. **This decision should be made explicitly before coding.**
2. **SDK churn.** 0.8 → 0.15 in eight months, with a breaking rename of ErrorPart/stream modes
   across versions already. Pin exactly (`==`), not `>=`, and expect to re-run this spike's
   tests on every bump — that is what `tests/spikes/test_a2ui_spike.py` is for.
3. **Catalog ownership.** The Basic Catalog is Google's. The moment we want a Quartz-specific
   component (a kernel cell, a job table, a diff view), we own a catalog: schema, versioning,
   and the renderer that draws it. That is the real long-term cost, and it grows with ambition.

## What we would be committing to

- A pinned Python dependency (5 packages) and a pinned JS renderer, upgraded together.
- A 23k-char prompt on A2UI-capable turns, regenerated per processor.
- Owning a catalog if we ever want components that are not Google's.
- Deciding the turn-mode question in risk 1.

## Recommended next step

Do **not** integrate yet. Run a second, smaller spike that answers only risk 1: a
`quartz web` turn that emits a single `a2ui` frame and renders it with `@a2ui/react` in the
existing SPA. That closes the one claim this spike could not test — that it actually paints —
and forces the turn-mode decision, before any of this reaches `main`.
