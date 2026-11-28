"""Concurrency and incremental-copy tests for Session JSONL persistence."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from quartz.core.session import Session


def test_concurrent_save_messages_and_meta_updates_preserve_all_records(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session = Session.create(tmp_path, sessions_dir=sessions_dir)
    messages: list[dict[str, object]] = [{"role": "user", "content": "seed"}]
    session.save_messages(messages)

    total_appends = 60
    errors: list[BaseException] = []
    start_barrier = threading.Barrier(3)

    def writer_messages() -> None:
        try:
            start_barrier.wait()
            for idx in range(total_appends):
                messages.append({"role": "assistant", "content": f"msg-{idx}"})
                session.save_messages(messages)
        except BaseException as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    def writer_name() -> None:
        try:
            start_barrier.wait()
            for idx in range(total_appends):
                session.set_name(f"Session {idx}")
        except BaseException as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    def writer_rich_ui() -> None:
        try:
            start_barrier.wait()
            for idx in range(total_appends):
                session.set_rich_ui(idx % 2 == 0)
        except BaseException as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer_messages),
        threading.Thread(target=writer_name),
        threading.Thread(target=writer_rich_ui),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    assert all(not t.is_alive() for t in threads)

    raw_lines = session.jsonl_path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in raw_lines]
    assert parsed[0]["type"] == "meta"

    loaded, loaded_msgs = Session.load(session.id, sessions_dir=sessions_dir)
    assert loaded_msgs == messages
    assert len(loaded_msgs) == total_appends + 1
    assert loaded.name == f"Session {total_appends - 1}"
    assert loaded.rich_ui is ((total_appends - 1) % 2 == 0)


def test_incremental_save_messages_preserves_persisted_ref_identities(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session = Session.create(tmp_path, sessions_dir=sessions_dir)

    messages: list[dict[str, object]] = [
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": "second"},
    ]
    session.save_messages(messages)
    ref0 = session._persisted_refs[0]
    ref1 = session._persisted_refs[1]

    # Append-only save must not deep-copy previously persisted entries.
    messages.append({"role": "user", "content": [{"type": "text", "text": "third"}]})
    session.save_messages(messages)

    assert session._persisted_refs[0] is ref0
    assert session._persisted_refs[1] is ref1
    assert len(session._persisted_refs) == 3
    ref2 = session._persisted_refs[2]

    # Mutating the newly appended message in-place must still be detected as prefix_changed.
    third_content = messages[2]["content"]
    assert isinstance(third_content, list)
    third_content[0]["text"] = "mutated-third"
    messages.append({"role": "assistant", "content": "fourth"})
    session.save_messages(messages)

    # Compaction branch rebuilds _persisted_refs and writes a compaction record.
    assert session._persisted_refs[2] is not ref2
    _, loaded_msgs = Session.load(session.id, sessions_dir=sessions_dir)
    assert loaded_msgs == messages
