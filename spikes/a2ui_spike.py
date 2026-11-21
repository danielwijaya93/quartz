"""SPIKE: drive `a2ui-agent-sdk` from Quartz's own provider layer.

A demonstration and a decision document, not a production feature. Nothing in the
package imports it; the verdict lives in `spikes/A2UI_SPIKE.md`.

Three claims are tested here, in increasing order of importance:

1. **The seam is real and small.** Quartz's `build_provider()` returns a client
   whose `stream(model, messages)` yields typed events, one of which is `TextDelta`.
   A2UI's processor consumes *text*, fed in with `feed(chunk)`. The adapter between
   them is `quartz_text_chunks` below: ~15 lines, and it needs nothing new from
   Quartz's provider layer.

2. **Valid output compiles to real A2UI envelopes.** A streamed Express-DSL block
   becomes a `createSurface` message carrying the catalog id and the component
   tree, in protocol version 1.0.

3. **Server-side validation rejects bad output with a usable error.** Naming a
   component the catalog lacks produces `A2uiCompilationValidationError` (or an
   `ErrorPart`, if the stream is opened in continue-mode) whose message names the
   offending component and the catalog. This is the capability that justified the
   full SDK over browser-side parsing: the error is a string you can hand back to
   the model for a retry, whereas a browser parser can only fail to paint.

Gotchas worth knowing before integrating (all learned here, none documented in the
README):

* The model output MUST be wrapped in the literal `<a2ui>` / `</a2ui>` sentinels.
  Without them the text is passed through as `TextPart` prose and compiles to
  nothing -- silently. This is the most likely way an integration appears broken
  for no reason.
* `open_stream(after_rejected_block=...)` chooses what a compile error does:
  `"end"` (default) raises out of `feed()`, `"continue"` returns an `ErrorPart` and
  keeps going. A self-correcting loop wants `"continue"`.
* `A2uiError` lives in `a2ui.parser.errors`, not `a2ui.errors`.

Run:

    env -u PYTHONPATH uv run python spikes/a2ui_spike.py            # no model needed
    env -u PYTHONPATH uv run python spikes/a2ui_spike.py --live     # real model

`--live` uses whatever provider and credentials Quartz itself is configured with
(via `build_provider`), so no secret is read in this file.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

from a2ui.core.basic_catalog import BasicCatalog
from a2ui.core.schema import ProtocolVersion
from a2ui.core.schema.v1_0.renderer_capabilities import (
    A2uiRendererCapabilities,
    V10Capabilities,
)
from a2ui.inference_formats import ExpressFormatFactory
from a2ui.parser import A2uiPart, ErrorPart
from a2ui.parser.errors import A2uiError
from a2ui.processor import A2uiGenerator, CatalogConfig
from a2ui.processor.processor import A2uiRequestProcessor

from quartz.providers.base import LLMProvider, TextDelta
from quartz.providers.selection import build_provider

# ---------------------------------------------------------------------------
# The adapter: Quartz's provider stream -> A2UI's text stream.
#
# This is the entire integration surface on the agent side. Quartz emits typed
# events; the Express compiler wants text. Thinking and tool-call events are
# dropped here rather than teaching either side about the other.
# ---------------------------------------------------------------------------


def quartz_text_chunks(
    client: LLMProvider,
    model: str,
    *,
    system_prompt: str,
    user_message: str,
    effort: str | None = None,
) -> Iterator[str]:
    """Yield the assistant's text for one exchange, as it arrives.

    A generator rather than a collected string, because the processor compiles
    incrementally: feeding a chunk at a time is what lets a form paint while the
    model is still writing.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    handle = None
    try:
        handle = client.stream(model, messages, None, effort=effort)
        for event in handle:
            if isinstance(event, TextDelta):
                yield event.text
    finally:
        # The provider holds an HTTP stream open; closing it releases the socket
        # even when the consumer stops early (a compile error, an aborted turn).
        if handle is not None:
            handle.close()


# ---------------------------------------------------------------------------
# Generator construction. A real integration hoists this into one long-lived
# object: building it costs a catalog parse (and a 23k-char prompt), not a call.
# ---------------------------------------------------------------------------


def build_processor() -> tuple[A2uiRequestProcessor, str]:
    """Return a v1.0 processor plus the catalog id a renderer must advertise.

    The capability handshake is the easy thing to get wrong. The *renderer* declares
    which catalogs it can draw; the agent then emits only components from those.
    `supported_catalog_ids` is required, and an empty list raises.
    """
    catalog = BasicCatalog(ProtocolVersion.V1_0)
    generator = A2uiGenerator(
        catalogs=[CatalogConfig(catalog=catalog)],
        inference_format_factory=ExpressFormatFactory(),
    )
    capabilities = A2uiRendererCapabilities(
        v1_0=V10Capabilities(supported_catalog_ids=[catalog.id])
    )
    return generator.create_processor(capabilities), catalog.id


# ---------------------------------------------------------------------------
# Compiling a stream.
# ---------------------------------------------------------------------------


def compile_chunks(
    processor: A2uiRequestProcessor,
    chunks: Iterator[str],
    *,
    continue_after_error: bool = False,
) -> list[Any]:
    """Feed text chunks through the processor; return every part it emitted.

    `continue_after_error=False` matches the user-facing default: a bad block
    raises. `True` turns a bad block into an `ErrorPart` and keeps reading, which
    is the mode a retry loop wants.
    """
    mode = "continue" if continue_after_error else "end"
    stream = processor.open_stream(after_rejected_block=mode)  # type: ignore[arg-type]
    parts: list[Any] = []
    for chunk in chunks:
        parts.extend(stream.feed(chunk))
    parts.extend(stream.close())
    return parts


def describe(parts: list[Any]) -> dict[str, Any]:
    """Summarise a compile result: which envelopes, and which errors."""
    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    text: list[str] = []
    for part in parts:
        if isinstance(part, A2uiPart):
            for message in part.a2ui:
                dumped = message.model_dump(by_alias=True, exclude_none=True)
                # The envelope key is whichever field is not the protocol version.
                envelope = next(k for k in dumped if k not in ("version", "a2ui"))
                messages.append({"type": envelope, "surfaceId": _surface_of(dumped)})
        elif isinstance(part, ErrorPart):
            errors.append(str(part.error))
        else:
            # Prose that sat outside a sentinel block; the transport renders it as
            # ordinary markdown alongside any UI.
            text.append(str(getattr(part, "text", "")))
    return {"messages": messages, "errors": errors, "text": "".join(text)}


def _surface_of(dumped: dict[str, Any]) -> str | None:
    """Pull the surface id out of whichever envelope carried it."""
    body = dumped.get("createSurface") or dumped.get("updateComponents")
    if isinstance(body, dict):
        return body.get("surfaceId")
    return dumped.get("surfaceId")


# ---------------------------------------------------------------------------
# Canned cases: no model, no credentials. CI can run these.
# ---------------------------------------------------------------------------

GOOD_BLOCK = (
    "<a2ui>\n"
    'surface("main")\n'
    'heading = Text("Quarterly sales")\n'
    'col = Column([heading])\n'
    'root = Card(col)\n'
    "</a2ui>"
)

BAD_BLOCK = (
    "<a2ui>\n"
    'surface("main")\n'
    'root = NotARealComponent("not in the catalog")\n'
    "</a2ui>"
)

UNWRAPPED_BLOCK = 'surface("main")\nroot = Card(Text("no sentinels"))\n'


def run_canned(processor: A2uiRequestProcessor) -> int:
    """Prove compile-on-valid, reject-on-invalid, and the sentinel requirement."""
    good = describe(compile_chunks(processor, _chunked(GOOD_BLOCK)))
    print("=== 1. valid block ===")
    print("raw:      ", GOOD_BLOCK.replace("\n", "\\n"))
    print("messages: ", good["messages"])
    print("errors:   ", good["errors"])

    rejected = describe(compile_chunks(processor, _chunked(BAD_BLOCK), continue_after_error=True))
    print("\n=== 2. unknown component (continue mode) ===")
    print("messages: ", rejected["messages"])
    print("errors:   ", rejected["errors"])

    unwrapped = describe(compile_chunks(processor, _chunked(UNWRAPPED_BLOCK)))
    print("\n=== 3. missing <a2ui> sentinels ===")
    print("messages: ", unwrapped["messages"], "(expected: none)")
    print("text:     ", repr(unwrapped["text"])[:80])

    ok = (
        bool(good["messages"])
        and not good["errors"]
        and bool(rejected["errors"])
        and not unwrapped["messages"]
    )
    print("\nRESULT:", "all three behaviours confirmed" if ok else "UNEXPECTED BEHAVIOUR")
    return 0 if ok else 1


def _chunked(text: str, size: int = 7) -> Iterator[str]:
    """Split into awkward little chunks, the way a token stream arrives."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


# ---------------------------------------------------------------------------
# Live case: a real model, through Quartz's own configuration.
# ---------------------------------------------------------------------------

LIVE_PROMPT = (
    "Show three sales KPIs as cards in a row: Revenue 1.2M, Growth 12%, Churn 3%. "
    "Keep it compact."
)


def run_live(processor: A2uiRequestProcessor) -> int:
    """Drive a real model through Quartz's provider and compile what it writes."""
    client, provider_name, model, effort = build_provider()
    print(f"provider={provider_name} model={model} effort={effort}")

    raw: list[str] = []

    def tee() -> Iterator[str]:
        for chunk in quartz_text_chunks(
            client,
            model,
            system_prompt=processor.prompt_snippet,
            user_message=LIVE_PROMPT,
            effort=effort,
        ):
            raw.append(chunk)
            yield chunk

    result = describe(compile_chunks(processor, tee(), continue_after_error=True))
    print("\n=== raw model output ===")
    print("".join(raw))
    print("\n=== compiled ===")
    print("messages:", result["messages"])
    print("errors:  ", result["errors"])

    ok = bool(result["messages"])
    print("\nRESULT:", "live compile produced UI" if ok else "live compile produced no UI")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    processor, catalog_id = build_processor()
    print(f"catalog:  {catalog_id}")
    print(f"prompt:   {len(processor.prompt_snippet)} chars generated from the catalog schema\n")
    if "--live" in argv:
        return run_live(processor)
    return run_canned(processor)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
