"""Tests for JSONL session persistence, compaction logging, and dill kernel snapshots."""

from __future__ import annotations

import os
import time
from pathlib import Path

from quartz.kernel.client import KernelClient
from quartz.core.session import Session, prune_snapshots


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


def test_snapshot_stores_aliased_objects_once(tmp_path: Path) -> None:
    """`rows2 = rows` is one payload; it must not be written twice."""
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        r = k1.exec_cell(
            "rows = [{'id': i, 'v': 'x' * 200} for i in range(500)]\n"
            "rows2 = rows\n"
            "rows3 = rows\n"
        )
        assert r.ok
        manifest = s.save_kernel(k1)
    finally:
        k1.close()

    files = manifest["files"]
    assert files["rows"] == files["rows2"] == files["rows3"]
    dill_files = [p.name for p in s.snapshot_dir.glob("*.dill")]
    assert len([n for n in dill_files if n.startswith("obj")]) == 1

    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        s.restore_kernel(k2)
        r2 = k2.exec_cell("(len(rows), rows2 is rows, rows3 is rows)")
        assert r2.ok
        assert r2.result == "(500, True, True)"
    finally:
        k2.close()


def test_restore_reads_legacy_name_keyed_snapshot(tmp_path: Path) -> None:
    """Snapshots written before dedup used `<name>.dill`; resume must still work."""
    import json

    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        assert k1.exec_cell("legacy_value = 41").ok
        s.save_kernel(k1)
    finally:
        k1.close()

    # Rewrite the snapshot in the old on-disk shape.
    for f in list(s.snapshot_dir.glob("*.dill")):
        f.rename(s.snapshot_dir / "legacy_value.dill")
    manifest = json.loads((s.snapshot_dir / "manifest.json").read_text())
    manifest.pop("files")
    manifest["saved"] = ["legacy_value"]
    (s.snapshot_dir / "manifest.json").write_text(json.dumps(manifest))

    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        notice = s.restore_kernel(k2)
        assert notice is not None and "legacy_value" in notice
        r2 = k2.exec_cell("legacy_value + 1")
        assert r2.ok and r2.result == "42"
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


def test_session_latest_filters_by_cwd_and_persists_usage(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    s_a = Session.create(dir_a, sessions_dir=sessions_dir)
    s_a.save_messages([{"role": "user", "content": "in dir_a"}])
    s_a.save_usage({"prompt_tokens": 4200, "cached_tokens": 1000, "total_prompt_tokens": 4200})

    s_b = Session.create(dir_b, sessions_dir=sessions_dir)
    s_b.save_messages([{"role": "user", "content": "in dir_b"}])

    # Latest for dir_a should still be s_a even though s_b is newer globally
    assert Session.latest_id(cwd=dir_a, sessions_dir=sessions_dir) == s_a.id
    assert Session.latest_id(cwd=dir_b, sessions_dir=sessions_dir) == s_b.id
    assert Session.latest_id(cwd=tmp_path, sessions_dir=sessions_dir) is None

    loaded_a, _ = Session.load(s_a.id, sessions_dir=sessions_dir)
    assert loaded_a.last_usage == {"prompt_tokens": 4200, "cached_tokens": 1000, "total_prompt_tokens": 4200}

    forked_a, _ = Session.fork(s_a.id, dir_a, sessions_dir=sessions_dir)
    reloaded_fork, _ = Session.load(forked_a.id, sessions_dir=sessions_dir)
    assert reloaded_fork.last_usage == loaded_a.last_usage


def test_session_list_for_cwd(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    s1 = Session.create(dir_a, sessions_dir=sessions_dir)
    s1.save_messages([{"role": "user", "content": "first in a"}, {"role": "assistant", "content": "ok"}])

    s_other = Session.create(dir_b, sessions_dir=sessions_dir)
    s_other.save_messages([{"role": "user", "content": "in b"}])

    s2 = Session.create(dir_a, sessions_dir=sessions_dir)
    s2.save_messages([{"role": "user", "content": "second in a"}])

    listed = Session.list_for_cwd(dir_a, sessions_dir=sessions_dir)
    assert [item["id"] for item in listed] == [s2.id, s1.id]
    assert listed[0]["preview"] == "second in a"
    assert listed[0]["message_count"] == 1
    assert listed[1]["preview"] == "first in a"
    assert listed[1]["message_count"] == 2


def test_session_save_messages_detects_in_place_and_grown_compaction(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    msgs = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "ipython", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "huge output " * 50},
        {"role": "assistant", "content": "done"},
    ]
    s.save_messages(msgs)

    # 1) Same-length CLEARED_TOOL_RESULTS style replacement without compacted=True
    cleared = [
        msgs[0],
        msgs[1],
        {"role": "tool", "tool_call_id": "tc1", "content": "[cleared]"},
        msgs[3],
    ]
    s.save_messages(cleared)
    _, reloaded = Session.load(s.id, sessions_dir=sessions_dir)
    assert reloaded == cleared

    # 2) SUMMARIZED-then-grown list (longer than _persisted_count=4) without compacted=True
    summarized_and_grown = [
        {"role": "user", "content": "[compaction-summary]"},
        {"role": "assistant", "content": "resumed"},
        {"role": "user", "content": "follow up 1"},
        {"role": "assistant", "content": "ok 1"},
        {"role": "user", "content": "follow up 2"},
    ]
    s.save_messages(summarized_and_grown)
    _, reloaded2 = Session.load(s.id, sessions_dir=sessions_dir)
    assert reloaded2 == summarized_and_grown


def _fake_session(root: Path, sid: str, *, age_days: float, payload: int = 1024) -> None:
    """Write a session transcript plus snapshot dir, aged `age_days` into the past."""
    (root / f"{sid}.jsonl").write_text('{"type": "meta"}\n')
    snap = root / f"{sid}.snapshot"
    snap.mkdir()
    (snap / "obj00000.dill").write_bytes(b"x" * payload)
    (snap / "manifest.json").write_text("{}")
    stamp = time.time() - age_days * 86400
    for f in [root / f"{sid}.jsonl", snap, snap / "obj00000.dill", snap / "manifest.json"]:
        os.utime(f, (stamp, stamp))


def test_prune_removes_old_snapshots_and_keeps_transcripts(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    _fake_session(root, "old", age_days=30, payload=4096)
    _fake_session(root, "fresh", age_days=1, payload=2048)

    result = prune_snapshots(root, older_than_days=7)

    assert result.snapshots_removed == 1
    assert result.bytes_freed > 0
    assert not (root / "old.snapshot").exists()
    assert (root / "old.jsonl").exists()          # transcript survives
    assert (root / "fresh.snapshot").is_dir()     # young enough to keep
    assert (root / "fresh.jsonl").exists()


def test_prune_dry_run_frees_nothing(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    _fake_session(root, "old", age_days=30, payload=4096)

    result = prune_snapshots(root, older_than_days=7, dry_run=True)

    assert result.snapshots_removed == 1
    assert result.bytes_freed >= 4096
    assert (root / "old.snapshot").is_dir()


def test_prune_zero_days_disables_pruning(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    _fake_session(root, "ancient", age_days=3650)

    assert prune_snapshots(root, older_than_days=0).snapshots_removed == 0
    assert (root / "ancient.snapshot").is_dir()


def test_prune_uses_transcript_mtime_over_snapshot_mtime(tmp_path: Path) -> None:
    """A session resumed yesterday keeps its snapshot even if the dir looks old."""
    root = tmp_path / "sessions"
    root.mkdir()
    _fake_session(root, "resumed", age_days=40)
    now = time.time()
    os.utime(root / "resumed.jsonl", (now, now))

    assert prune_snapshots(root, older_than_days=7).snapshots_removed == 0
    assert (root / "resumed.snapshot").is_dir()


def test_prune_missing_dir_is_not_an_error(tmp_path: Path) -> None:
    assert prune_snapshots(tmp_path / "nope", older_than_days=7).snapshots_removed == 0
