"""Tests for JSONL session persistence, compaction logging, and dill kernel snapshots."""

from __future__ import annotations

from pathlib import Path

from agent.kernel_client import KernelClient
from agent.session import Session


def test_session_jsonl_save_load_and_compaction(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    s.save_messages(msgs)

    msgs.append({"role": "user", "content": "next"})
    s.save_messages(msgs)

    loaded_s, loaded_msgs = Session.load(s.id, sessions_dir=sessions_dir)
    assert loaded_s.id == s.id
    assert loaded_msgs == msgs

    # Simulate compaction replacing older messages
    compacted = [{"role": "user", "content": "[compaction-summary]"}, {"role": "user", "content": "next"}]
    loaded_s.save_messages(compacted, compacted=True)

    _, reloaded_msgs = Session.load(s.id, sessions_dir=sessions_dir)
    assert reloaded_msgs == compacted


def test_session_dill_snapshot_and_restore(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        r = k1.exec_cell(
            "data = {'a': 1, 'b': [2, 3]}\n"
            "def add(x, y): return x + y\n"
            "unpicklable = (i for i in range(3))\n"
        )
        assert r.ok
        manifest = s.save_kernel(k1)
        assert "data" in manifest["saved"]
        assert "add" in manifest["saved"]
        assert any("unpicklable" in item for item in manifest["skipped"])
    finally:
        k1.close()

    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        notice = s.restore_kernel(k2)
        assert notice is not None
        assert "[python-state-restored]" in notice
        assert "data" in notice
        assert "add" in notice

        r2 = k2.exec_cell("add(data['a'], data['b'][1])")
        assert r2.ok
        assert r2.result == "4"
    finally:
        k2.close()


def test_session_fork_and_latest(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s1 = Session.create(tmp_path, sessions_dir=sessions_dir)
    s1.save_messages([{"role": "user", "content": "one"}])

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        k1.exec_cell("val = 99")
        s1.save_kernel(k1)
    finally:
        k1.close()

    assert Session.latest_id(sessions_dir=sessions_dir) == s1.id

    forked, f_msgs = Session.fork(s1.id, tmp_path, sessions_dir=sessions_dir)
    assert forked.id != s1.id
    assert f_msgs == [{"role": "user", "content": "one"}]

    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        forked.restore_kernel(k2)
        r = k2.exec_cell("val")
        assert r.ok and r.result == "99"
    finally:
        k2.close()


def test_session_lazy_create_and_latest_skips_empty(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s1 = Session.create(tmp_path, sessions_dir=sessions_dir)
    s1.save_messages([{"role": "user", "content": "real message"}])

    # Creating a new session without saving messages should not write a file
    s_unused = Session.create(tmp_path, sessions_dir=sessions_dir)
    assert not s_unused.jsonl_path.exists()

    # Even if an empty meta-only .jsonl exists with newer mtime, latest_id skips it
    empty_file = sessions_dir / "99999999-999999-empty0.jsonl"
    empty_file.write_text('{"type": "meta", "id": "empty"}\n', encoding="utf-8")
    assert Session.latest_id(sessions_dir=sessions_dir) == s1.id
