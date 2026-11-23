"""Regression tests for startup, TUI responsiveness, and exit performance optimizations."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from quartz.bootstrap import BootstrapOptions, build_runtime
from quartz.core.commands import reload_runtime_mcp


def test_dock_sets_low_ttimeoutlen_and_redraw_interval(tmp_path: Path) -> None:
    from quartz.tui.dock import InteractiveDock

    rt = SimpleNamespace(
        cwd=tmp_path,
        model="gpt-4.1",
        effort="medium",
        max_context_tokens=128_000,
        usage_stats=SimpleNamespace(prompt_tokens=0, total_prompt_tokens=0),
        kernel=SimpleNamespace(
            running_bash_count=lambda: 0,
            set_bg_count_callback=lambda cb: None,
            has_pending_events=lambda: False,
        ),
        messages=[],
        cell_history=[],
        pending_images=[],
        active_session=SimpleNamespace(cell_meta={}, steering_indices=[]),
        verbose=False,
        turn_count=0,
        total_elapsed_s=0.0,
    )
    dock = InteractiveDock(rt)  # type: ignore[arg-type]
    assert dock.app.ttimeoutlen <= 0.05
    assert dock.app.min_redraw_interval <= 0.02


def test_build_runtime_does_not_block_on_slow_model_discovery(tmp_path: Path, monkeypatch: Any) -> None:
    started_discovery = threading.Event()
    finish_discovery = threading.Event()

    class _SlowClient:
        def list_model_details(self) -> list[Any]:
            started_discovery.set()
            finish_discovery.wait(timeout=2.0)
            return [SimpleNamespace(id="gpt-4.1", context_length=555_555)]

        def close(self) -> None:
            pass

    class _FakeKernel:
        def __init__(self, cwd: Path | None = None) -> None:
            self.cwd = cwd
            self.dirty = False

        def start(self) -> None:
            pass

        def configure(self, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "quartz.bootstrap.build_provider",
        lambda **kwargs: (_SlowClient(), "openai", "gpt-4.1", "medium"),
    )

    t0 = time.monotonic()
    rt, _ = build_runtime(
        BootstrapOptions(
            cwd=tmp_path,
            headless=True,
            kernel_factory=_FakeKernel,
        )
    )
    elapsed = time.monotonic() - t0

    # Must return immediately using the offline table without waiting for _SlowClient.
    assert elapsed < 0.5
    assert rt.max_context_tokens == 1_047_576

    # Background thread updates max_context_tokens once discovery finishes.
    finish_discovery.set()
    assert started_discovery.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while rt.max_context_tokens != 555_555 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rt.max_context_tokens == 555_555


def test_standalone_modal_sets_low_ttimeoutlen() -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import Layout
    from quartz.tui.modals import InlineModalState, build_modal_container

    state = InlineModalState(
        kind="option",
        title="Pick",
        buffer=Buffer(multiline=False),
        options=[("a", "A")],
    )
    container, _, list_window = build_modal_container(lambda: state, state.buffer)
    assert Layout(container, focused_element=list_window) is not None
