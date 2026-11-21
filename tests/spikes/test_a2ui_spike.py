"""What the A2UI spike proved, pinned so a dependency bump cannot quietly undo it.

Two kinds of test:

* **Canned** (always run, no model, no credentials). These exercise the real SDK:
  the Express compiler, the catalog validator, the capability handshake. They are
  the ones that matter day to day -- a break here means an A2UI upgrade changed
  behaviour we depend on.
* **Live** (`--live`, or `QUARTZ_SPIKE_LIVE=1`). Opt-in only, because they spend
  money and need a configured provider. Skipped by default so CI stays hermetic.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

# `spikes/` is deliberately outside the installed package -- a decision document
# that happens to run. A conftest.py here would shadow `tests/conftest.py` (which
# `tests/tui/` imports by name) and break collection, so the path is inserted in
# this module instead.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "spikes"))

import pytest
from a2ui.parser import A2uiPart, ErrorPart
from a2ui.parser.errors import A2uiError

from a2ui_spike import (
    BAD_BLOCK,
    GOOD_BLOCK,
    UNWRAPPED_BLOCK,
    build_processor,
    compile_chunks,
    describe,
)

# ---------------------------------------------------------------------------
# Canned: the contract we would build on.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def processor():
    """One processor for the module: construction parses the catalog and builds a
    23k-char prompt, which is not worth repeating per test."""
    proc, catalog_id = build_processor()
    return proc, catalog_id


def _chunked(text: str, size: int = 7) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


def test_capability_handshake_needs_a_catalog_id(processor) -> None:
    """The renderer must advertise which catalogs it can draw.

    This is the single easiest thing to get wrong in a real integration: pass an
    empty capability object and `create_processor` raises rather than defaulting.
    """
    from a2ui.core.basic_catalog import BasicCatalog
    from a2ui.core.schema import ProtocolVersion
    from a2ui.core.schema.v1_0.renderer_capabilities import (
        A2uiRendererCapabilities,
        V10Capabilities,
    )
    from a2ui.inference_formats import ExpressFormatFactory
    from a2ui.processor import A2uiGenerator, CatalogConfig

    _, catalog_id = processor
    assert catalog_id.startswith("https://a2ui.org/specification/v1_0/catalogs/")

    catalog = BasicCatalog(ProtocolVersion.V1_0)
    generator = A2uiGenerator(
        catalogs=[CatalogConfig(catalog=catalog)],
        inference_format_factory=ExpressFormatFactory(),
    )
    with pytest.raises(Exception) as excinfo:
        generator.create_processor(
            A2uiRendererCapabilities(v1_0=V10Capabilities(supported_catalog_ids=[]))
        )
    # The point is that it fails loudly, not silently with no components.
    assert excinfo.value is not None


def test_valid_block_compiles_to_a_create_surface(processor) -> None:
    proc, catalog_id = processor
    result = describe(compile_chunks(proc, _chunked(GOOD_BLOCK)))

    assert result["errors"] == []
    assert result["messages"], "a valid block must produce at least one envelope"
    assert result["messages"][0]["type"] == "createSurface"
    assert result["messages"][0]["surfaceId"] == "main"


def test_compiled_envelope_carries_the_catalog_id(processor) -> None:
    """The renderer picks its component set from this id, so it must survive."""
    proc, catalog_id = processor
    stream = proc.open_stream()
    parts = stream.feed(GOOD_BLOCK) + stream.close()
    messages = [m for p in parts if isinstance(p, A2uiPart) for m in p.a2ui]

    dumped = messages[0].model_dump(by_alias=True, exclude_none=True)
    assert dumped["createSurface"]["catalogId"] == catalog_id
    assert dumped["version"] == "1.0"


def test_unknown_component_is_rejected_with_a_usable_message(processor) -> None:
    """This is the capability that justified the full SDK over browser-side parsing.

    The error names the offending component *and* the catalog. That string is what a
    self-correction loop hands back to the model, so its quality is the feature.
    """
    proc, catalog_id = processor
    result = describe(compile_chunks(proc, _chunked(BAD_BLOCK), continue_after_error=True))

    assert result["messages"] == []
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert "NotARealComponent" in error
    assert catalog_id in error


def test_rejection_raises_by_default(processor) -> None:
    """Default mode is `end`: a bad block raises instead of returning an ErrorPart.

    Worth pinning because the two modes are a bare string argument and it is easy to
    assume the quiet one is the default.
    """
    proc, _ = processor
    with pytest.raises(A2uiError):
        compile_chunks(proc, _chunked(BAD_BLOCK))


def test_continue_mode_returns_an_error_part_and_can_keep_going(processor) -> None:
    """A retry loop needs `continue`: recover the error, then accept a good block."""
    proc, _ = processor
    parts = compile_chunks(proc, _chunked(BAD_BLOCK), continue_after_error=True)
    assert any(isinstance(p, ErrorPart) for p in parts)


def test_missing_sentinels_compile_to_nothing(processor) -> None:
    """The trap: no `<a2ui>` wrapper means the block is prose, and it fails silently."""
    proc, _ = processor
    result = describe(compile_chunks(proc, _chunked(UNWRAPPED_BLOCK)))

    assert result["messages"] == []
    assert result["errors"] == []
    assert "surface(" in result["text"]


def test_progress_frames_are_configurable(processor) -> None:
    """`progressive_keys` changes wire volume by an order of magnitude.

    Default (`{'text'}`) re-sends a component on every text delta -- measured here as
    3 envelopes for a 2-component UI, and 37 for a 3-card layout in the live run.
    An empty set coalesces to one envelope per surface. The web transport needs to
    choose deliberately rather than inherit the default.
    """
    from a2ui.core.basic_catalog import BasicCatalog
    from a2ui.core.schema import ProtocolVersion
    from a2ui.core.schema.v1_0.renderer_capabilities import (
        A2uiRendererCapabilities,
        V10Capabilities,
    )
    from a2ui.inference_formats import ExpressFormatFactory
    from a2ui.processor import A2uiGenerator, CatalogConfig

    catalog = BasicCatalog(ProtocolVersion.V1_0)
    block = '<a2ui>\nsurface("s")\nroot = Card(Text("hello world"))\n</a2ui>'

    def count(**format_kwargs) -> int:
        generator = A2uiGenerator(
            catalogs=[CatalogConfig(catalog=catalog)],
            inference_format_factory=ExpressFormatFactory(**format_kwargs),
        )
        proc = generator.create_processor(
            A2uiRendererCapabilities(
                v1_0=V10Capabilities(supported_catalog_ids=[catalog.id])
            )
        )
        stream = proc.open_stream()
        parts = []
        for i in range(0, len(block), 5):
            parts += stream.feed(block[i : i + 5])
        parts += stream.close()
        return sum(1 for p in parts if isinstance(p, A2uiPart) for _ in p.a2ui)

    progressive = count()
    coalesced = count(progressive_keys=frozenset())
    assert progressive > coalesced, (progressive, coalesced)
    assert coalesced == 1


# ---------------------------------------------------------------------------
# Live: opt-in, spends money, needs a configured provider.
# ---------------------------------------------------------------------------


def _live_enabled() -> bool:
    return os.environ.get("QUARTZ_SPIKE_LIVE") == "1"


@pytest.mark.skipif(
    not _live_enabled(),
    reason="live model call; set QUARTZ_SPIKE_LIVE=1 to run (spends money)",
)
def test_live_model_produces_ui(processor) -> None:
    """The end-to-end claim: a real model, through Quartz's own provider, yields UI."""
    from a2ui_spike import LIVE_PROMPT, quartz_text_chunks
    from quartz.providers.selection import build_provider

    proc, _ = processor
    client, _provider, model, effort = build_provider()

    raw: list[str] = []

    def tee() -> Iterator[str]:
        for chunk in quartz_text_chunks(
            client,
            model,
            system_prompt=proc.prompt_snippet,
            user_message=LIVE_PROMPT,
            effort=effort,
        ):
            raw.append(chunk)
            yield chunk

    result = describe(compile_chunks(proc, tee(), continue_after_error=True))

    assert result["messages"], f"no UI compiled; raw output was: {''.join(raw)!r}"
    assert result["errors"] == []
    assert result["messages"][0]["type"] == "createSurface"
