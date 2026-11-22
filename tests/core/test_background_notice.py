"""A running background job reaches the session as a `[bash-status]` notice.

The clock lives in the worker (see ``tests/kernel/test_background_clock.py``); what
matters here is that the notice lands in ``messages`` the model reads, and that a
replayed session renders it as a harness notice instead of a user bubble.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.core.loop import drain_bash_events
from quartz.kernel.client import KernelClient


def test_drain_appends_a_status_notice(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        assert kernel.exec_cell("bg = bash('echo alive; sleep 2')").ok

        messages: list[dict] = []
        notice = None
        for _ in range(120):
            notice = drain_bash_events(kernel, messages)
            if notice and "[bash-status" in notice:
                break
            import time

            time.sleep(0.05)

        assert notice is not None and "[bash-status" in notice
        # Injected through the only role the harness may use, with the tag the
        # system prompt documents ("messages starting with [<kind>] come from the harness").
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith("[bash-status pid:")
        assert "command: echo alive; sleep 2" in messages[-1]["content"]
        assert "alive" in messages[-1]["content"]
    finally:
        kernel.close()


def test_drain_reports_done_after_status(tmp_path: Path) -> None:
    kernel = KernelClient(cwd=tmp_path)
    kernel.start()
    try:
        kernel.configure(background_check_every=0.4)
        assert kernel.exec_cell("bg = bash('sleep 1')").ok
        messages: list[dict] = []
        kinds: list[str] = []
        import time

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            notice = drain_bash_events(kernel, messages)
            if notice:
                kinds.append("done" if "[bash-done" in notice else "status")
            if kinds and kinds[-1] == "done":
                break
            time.sleep(0.05)
        # Status first, then the completion: a status is never the last word.
        assert "status" in kinds
        assert kinds[-1] == "done"
    finally:
        kernel.close()


def test_replay_renders_a_status_notice_as_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed session must not attribute the harness's words to the user."""
    from quartz.tui import replay as replay_mod

    rendered: list[str] = []

    class _Printer:
        def reset(self) -> None: ...
        def block(self, *args, **kwargs) -> None: ...
        def flush(self) -> None: ...

    monkeypatch.setattr(replay_mod, "_active_printer", lambda: _Printer())
    monkeypatch.setattr(
        replay_mod, "_render_bash_notice", lambda text: rendered.append(text)
    )
    monkeypatch.setattr(
        replay_mod, "render_user_prompt", lambda text: rendered.append(f"USER:{text}")
    )

    replay_mod._replay_session(
        [
            {
                "role": "user",
                "content": (
                    "[bash-status pid:42 elapsed:5m02s]\ncommand: make\ntail:\nbuilding\n"
                ),
            }
        ],
        [],
    )
    assert rendered and rendered[0].startswith("[bash-status")
    assert not any(r.startswith("USER:") for r in rendered)


def test_replay_still_renders_done_and_progress_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a notice kind must not drop the ones that were already handled."""
    from quartz.tui import replay as replay_mod

    rendered: list[str] = []

    class _Printer:
        def reset(self) -> None: ...
        def block(self, *args, **kwargs) -> None: ...
        def flush(self) -> None: ...

    monkeypatch.setattr(replay_mod, "_active_printer", lambda: _Printer())
    monkeypatch.setattr(replay_mod, "_render_bash_notice", lambda text: rendered.append(text))
    monkeypatch.setattr(
        replay_mod, "render_user_prompt", lambda text: rendered.append(f"USER:{text}")
    )

    replay_mod._replay_session(
        [
            {"role": "user", "content": "[bash-done pid:1 exit:0 duration:2s]\ncommand: make\n"},
            {"role": "user", "content": "[progress] Update the user now on the progress."},
            {"role": "user", "content": "a real message"},
        ],
        [],
    )
    assert rendered[0].startswith("[bash-done")
    assert rendered[1].startswith("[progress]")
    assert rendered[2] == "USER:a real message"
