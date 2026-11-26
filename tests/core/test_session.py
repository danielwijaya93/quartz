"""Tests for JSONL session persistence, compaction logging, and dill kernel snapshots."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from quartz.core.session import Session, prune_snapshots
from quartz.kernel.client import KernelClient


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


async def test_session_dill_snapshot_and_restore(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        r = await k1.exec_cell(
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

        r2 = await k2.exec_cell("add(data['a'], data['b'][1])")
        assert r2.ok
        assert r2.result == "4"
    finally:
        k2.close()


async def test_snapshot_stores_aliased_objects_once(tmp_path: Path) -> None:
    """`rows2 = rows` is one payload; it must not be written twice."""
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        r = await k1.exec_cell(
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
    obj_files = [p.name for p in s.snapshot_dir.glob("obj*")]
    assert len(obj_files) == 1

    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        s.restore_kernel(k2)
        r2 = await k2.exec_cell("(len(rows), rows2 is rows, rows3 is rows)")
        assert r2.ok
        assert r2.result == "(500, True, True)"
    finally:
        k2.close()


async def test_restore_reads_legacy_name_keyed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshots written before dedup used `<name>.dill`; opt-in resume must still work."""
    import json
    import pickle

    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    # Write a legacy pre-dedup, pre-signing `<name>.dill` snapshot directly.
    s.snapshot_dir.mkdir(parents=True, exist_ok=True)
    (s.snapshot_dir / "legacy_value.dill").write_bytes(pickle.dumps(41, protocol=pickle.HIGHEST_PROTOCOL))
    manifest = {
        "saved": ["legacy_value"],
        "skipped": [],
        "modules": {},
        "cwd": str(tmp_path),
    }
    (s.snapshot_dir / "manifest.json").write_text(json.dumps(manifest))

    monkeypatch.setenv("QUARTZ_ALLOW_UNSIGNED_SNAPSHOTS", "1")
    k2 = KernelClient(cwd=tmp_path)
    k2.start()
    try:
        notice = s.restore_kernel(k2)
        assert notice is not None and "legacy_value" in notice
        r2 = await k2.exec_cell("legacy_value + 1")
        assert r2.ok and r2.result == "42"
    finally:
        k2.close()


async def test_session_fork_and_latest(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s1 = Session.create(tmp_path, sessions_dir=sessions_dir)
    s1.save_messages([{"role": "user", "content": "one"}])

    k1 = KernelClient(cwd=tmp_path)
    k1.start()
    try:
        await k1.exec_cell("val = 99")
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
        r = await k2.exec_cell("val")
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


# -- origin -------------------------------------------------------------


def test_created_session_records_its_origin(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    web = Session.create(tmp_path, sessions_dir=sessions_dir, origin="web")
    cli = Session.create(tmp_path, sessions_dir=sessions_dir)
    for session in (web, cli):
        session.save_messages([{"role": "user", "content": "hi"}])

    meta = json.loads((sessions_dir / f"{web.id}.jsonl").read_text().splitlines()[0])
    assert meta["origin"] == "web"
    # The terminal path is the default, and it is written explicitly so a reader
    # never has to know which fields were optional when the file was created.
    assert json.loads((sessions_dir / f"{cli.id}.jsonl").read_text().splitlines()[0])["origin"] == "cli"

    loaded_web, _ = Session.load(web.id, sessions_dir=sessions_dir)
    assert loaded_web.origin == "web"


def test_unknown_origin_is_stamped_as_cli(tmp_path: Path) -> None:
    """A typo must not create a third origin the sidebar cannot reason about."""
    sessions_dir = tmp_path / "sessions"
    session = Session.create(tmp_path, sessions_dir=sessions_dir, origin="browser")
    assert session.origin == "cli"


def test_session_without_origin_reads_as_cli(tmp_path: Path) -> None:
    """Sessions written before `origin` existed must not crash or look like web."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    legacy = sessions_dir / "20200101-000000-legacy.jsonl"
    legacy.write_text(
        json.dumps({"type": "meta", "id": "legacy", "cwd": str(tmp_path)}) + "\n"
        + json.dumps({"type": "message", "message": {"role": "user", "content": "old"}}) + "\n",
        encoding="utf-8",
    )

    rows, _ = Session.list_all(sessions_dir=sessions_dir)
    assert [row["origin"] for row in rows] == ["cli"]

    loaded, _ = Session.load("20200101-000000-legacy", sessions_dir=sessions_dir)
    assert loaded.origin == "cli"


def test_list_all_surfaces_origin_and_whether_the_cwd_still_exists(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    here = tmp_path / "here"
    here.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()

    present = Session.create(here, sessions_dir=sessions_dir, origin="web")
    present.save_messages([{"role": "user", "content": "in here"}])
    vanished = Session.create(gone, sessions_dir=sessions_dir)
    vanished.save_messages([{"role": "user", "content": "in a removed worktree"}])
    # A worktree removal, which is exactly how a workspace goes missing.
    gone.rmdir()

    rows, _ = Session.list_all(sessions_dir=sessions_dir)
    by_id = {row["sessionId"]: row for row in rows}
    assert by_id[present.id]["origin"] == "web"
    assert by_id[present.id]["cwdExists"] is True
    assert by_id[vanished.id]["origin"] == "cli"
    assert by_id[vanished.id]["cwdExists"] is False


def test_list_all_without_a_limit_returns_every_session(tmp_path: Path) -> None:
    """The web sidebar draws folders, not pages: it asks for all of them."""
    sessions_dir = tmp_path / "sessions"
    for index in range(3):
        session = Session.create(tmp_path, sessions_dir=sessions_dir)
        session.save_messages([{"role": "user", "content": f"msg {index}"}])

    rows, cursor = Session.list_all(sessions_dir=sessions_dir, limit=None)
    assert len(rows) == 3
    assert cursor is None


def test_fork_keeps_the_origin_of_its_source(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    source = Session.create(tmp_path, sessions_dir=sessions_dir, origin="web")
    source.save_messages([{"role": "user", "content": "one"}])

    forked, _ = Session.fork(source.id, tmp_path, sessions_dir=sessions_dir)
    assert forked.origin == "web"


def test_session_id_rejects_path_traversal(tmp_path: Path) -> None:
    import pytest

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    secret = tmp_path / "secret.jsonl"
    secret.write_text('{"type": "meta", "cwd": "/tmp"}\n', encoding="utf-8")

    # Symlink inside sessions_dir pointing outside must also be rejected.
    escape_link = sessions_dir / "escape.jsonl"
    os.symlink(secret, escape_link)

    bad_ids = [
        "../secret",
        "/abs/path",
        str(secret.with_suffix("")),
        "foo/bar",
        "foo\\bar",
        ".",
        "..",
        "",
        "   ",
        "bad\x00id",
        "escape",
    ]
    for bad_id in bad_ids:
        with pytest.raises(ValueError):
            Session(bad_id, sessions_dir=sessions_dir)
        with pytest.raises(ValueError):
            Session.load(bad_id, sessions_dir=sessions_dir)
        with pytest.raises(ValueError):
            Session.fork(bad_id, tmp_path, sessions_dir=sessions_dir)
        with pytest.raises(ValueError):
            Session.recorded_cwd(bad_id, sessions_dir=sessions_dir)


def test_save_messages_clears_steering_indices_on_compaction(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "steer"},
    ]
    s.record_steering(2)
    s.save_messages(msgs)
    assert s.steering_indices == {2}

    compacted_msgs = [{"role": "user", "content": "[compacted summary]"}]
    s.save_messages(compacted_msgs, compacted=True)
    assert s.steering_indices == set()

    loaded, _ = Session.load(s.id, sessions_dir=sessions_dir)
    assert loaded.steering_indices == set()


def test_list_all_includes_preview_for_forked_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    source = Session.create(tmp_path, sessions_dir=sessions_dir)
    source.save_messages(
        [
            {"role": "user", "content": "Explain the compaction preview"},
            {"role": "assistant", "content": "Sure."},
        ]
    )
    forked, _ = Session.fork(source.id, tmp_path, sessions_dir=sessions_dir)

    rows, _ = Session.list_all(sessions_dir=sessions_dir)
    by_id = {row["sessionId"]: row for row in rows}
    assert by_id[forked.id]["title"] == "Explain the compaction preview"


def test_load_repairs_dangling_tool_calls_and_heals_on_next_save(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)
    s.save_messages(
        [
            {"role": "user", "content": "First"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc_missing", "type": "function", "function": {"name": "ipython", "arguments": "{}"}}],
            },
            {"role": "user", "content": "Continue"},
        ]
    )

    loaded, msgs = Session.load(s.id, sessions_dir=sessions_dir)
    assert [m["role"] for m in msgs] == ["user", "user"]
    assert [m["content"] for m in msgs] == ["First", "Continue"]

    msgs.append({"role": "assistant", "content": "Back on track."})
    loaded.save_messages(msgs)

    _, reloaded = Session.load(s.id, sessions_dir=sessions_dir)
    assert [m["role"] for m in reloaded] == ["user", "user", "assistant"]


def test_session_records_and_updates_sandbox_in_meta(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    s = Session.create(tmp_path, sessions_dir=sessions_dir)

    assert Session.recorded_sandbox(s.id, sessions_dir=sessions_dir) is None
    assert Session.recorded_sandbox("missing-id", sessions_dir=sessions_dir) is None

    # Setting sandbox before the first message is saved updates pending meta.
    s.set_sandbox(False)
    s.save_messages([{"role": "user", "content": "hi"}])
    assert Session.recorded_sandbox(s.id, sessions_dir=sessions_dir) is False

    # Updating sandbox after the JSONL exists rewrites the meta line on disk.
    s.set_sandbox(True)
    assert Session.recorded_sandbox(s.id, sessions_dir=sessions_dir) is True

    # Corrupt JSONL returns None gracefully.
    (sessions_dir / "corrupt.jsonl").write_text("{not json\n", encoding="utf-8")
    assert Session.recorded_sandbox("corrupt", sessions_dir=sessions_dir) is None
