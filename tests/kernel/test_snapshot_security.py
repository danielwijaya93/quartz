"""Tests for hardened JSON-first + HMAC-signed kernel snapshots."""

from __future__ import annotations

import json
import os
import pickle
import stat
from pathlib import Path
from typing import Any

import dill
import pytest

from quartz.kernel.snapshots import (
    is_json_safe,
    load_or_create_snapshot_key,
    sha256_hex,
    verify_manifest_hmac,
)
from quartz.kernel.worker import KernelWorker


def _make_worker(tmp_path: Path) -> KernelWorker:
    os.chdir(tmp_path)
    return KernelWorker()


def test_is_json_safe_types_and_circular_refs() -> None:
    assert is_json_safe(None) is True
    assert is_json_safe(True) is True
    assert is_json_safe(42) is True
    assert is_json_safe(3.14) is True
    assert is_json_safe("hello") is True
    assert is_json_safe([1, "two", {"three": [True, None, 4.5]}]) is True

    # Shared non-circular reference (DAG) is JSON-safe
    shared = [1, 2]
    assert is_json_safe([shared, shared]) is True

    # Non-finite floats are rejected
    assert is_json_safe(float("nan")) is False
    assert is_json_safe(float("inf")) is False
    assert is_json_safe(float("-inf")) is False

    # Circular reference is rejected
    circ_list: list[Any] = []
    circ_list.append(circ_list)
    assert is_json_safe(circ_list) is False

    circ_dict: dict[str, Any] = {}
    circ_dict["self"] = circ_dict
    assert is_json_safe(circ_dict) is False

    # Non-str dict keys, tuples, sets, functions are rejected
    assert is_json_safe({1: "a"}) is False
    assert is_json_safe((1, 2)) is False
    assert is_json_safe({1, 2}) is False
    assert is_json_safe(lambda x: x) is False


def test_load_or_create_snapshot_key_and_readonly_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUARTZ_SNAPSHOT_KEY", raising=False)
    key_file = tmp_path / "keys" / "snapshot-key"
    monkeypatch.setenv("QUARTZ_SNAPSHOT_KEY_FILE", str(key_file))

    key1 = load_or_create_snapshot_key()
    assert key1 is not None and len(key1) == 32
    assert key_file.is_file()
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600

    key2 = load_or_create_snapshot_key()
    assert key2 == key1

    # Read-only parent directory when key does not exist returns None gracefully
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        missing_key = ro_dir / "sub" / "snapshot-key"
        assert load_or_create_snapshot_key(missing_key) is None
    finally:
        ro_dir.chmod(0o755)


def test_plain_data_saved_as_json_and_restored_without_pickle_or_dill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_SNAPSHOT_KEY", "01" * 32)
    snap_dir = tmp_path / "snap"

    w1 = _make_worker(tmp_path)
    w1._namespace["n"] = 42
    w1._namespace["s"] = "hello"
    w1._namespace["lst"] = [1, 2, 3]
    w1._namespace["d"] = {"a": True, "b": None, "c": 2.5}
    manifest = w1.snapshot_vars(str(snap_dir))

    files = manifest["files"]
    for name in ("n", "s", "lst", "d"):
        assert files[name].endswith(".json")
        assert (snap_dir / files[name]).is_file()
        assert manifest["hashes"][files[name]] == sha256_hex((snap_dir / files[name]).read_bytes())
    assert "hmac" in manifest

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pickle.loads / dill.loads must not be called for JSON snapshots")

    monkeypatch.setattr(pickle, "loads", _boom)
    monkeypatch.setattr(dill, "loads", _boom)

    w2 = _make_worker(tmp_path)
    res = w2.restore_vars(str(snap_dir))
    assert set(res["restored"]) == {"n", "s", "lst", "d"}
    assert res["skipped"] == []
    assert w2._namespace["n"] == 42
    assert w2._namespace["s"] == "hello"
    assert w2._namespace["lst"] == [1, 2, 3]
    assert w2._namespace["d"] == {"a": True, "b": None, "c": 2.5}


def test_complex_objects_saved_as_dill_with_hashes_and_hmac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_SNAPSHOT_KEY", "ab" * 32)
    snap_dir = tmp_path / "snap"

    w1 = _make_worker(tmp_path)
    w1._namespace["tup"] = (1, 2, 3)
    w1._namespace["fn"] = lambda x: x + 10
    manifest = w1.snapshot_vars(str(snap_dir))

    assert manifest["files"]["tup"].endswith(".dill")
    assert manifest["files"]["fn"].endswith(".dill")
    assert "hmac" in manifest
    assert verify_manifest_hmac(manifest, bytes.fromhex("ab" * 32))

    w2 = _make_worker(tmp_path)
    res = w2.restore_vars(str(snap_dir))
    assert set(res["restored"]) == {"tup", "fn"}
    assert w2._namespace["tup"] == (1, 2, 3)
    assert w2._namespace["fn"](5) == 15


def test_tampered_dill_bytes_or_manifest_fails_closed_without_unpickling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUARTZ_SNAPSHOT_KEY", "cd" * 32)
    snap_dir = tmp_path / "snap"

    w1 = _make_worker(tmp_path)
    w1._namespace["tup"] = (1, 2, 3)
    w1._namespace["plain"] = {"ok": True}
    manifest = w1.snapshot_vars(str(snap_dir))

    def _forbid_unpickle(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unpickler must never be called on tampered snapshot")

    monkeypatch.setattr(pickle, "loads", _forbid_unpickle)
    monkeypatch.setattr(dill, "loads", _forbid_unpickle)

    # 1. Tamper with the .dill file bytes
    dill_file = snap_dir / manifest["files"]["tup"]
    orig_bytes = dill_file.read_bytes()
    dill_file.write_bytes(orig_bytes + b"tamper")

    w2 = _make_worker(tmp_path)
    res1 = w2.restore_vars(str(snap_dir))
    assert "tup (tampered snapshot)" in res1["skipped"]
    assert "tup" not in w2._namespace
    assert w2._namespace["plain"] == {"ok": True}

    # Restore .dill bytes, then tamper with manifest.json
    dill_file.write_bytes(orig_bytes)
    manifest_path = snap_dir / "manifest.json"
    tampered_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tampered_manifest["cwd"] = str(tmp_path / "other")
    manifest_path.write_text(json.dumps(tampered_manifest), encoding="utf-8")

    w3 = _make_worker(tmp_path)
    res2 = w3.restore_vars(str(snap_dir))
    assert "tup (tampered snapshot)" in res2["skipped"]
    assert "tup" not in w3._namespace


def test_strict_snapshots_rejects_unsigned_dill_but_restores_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUARTZ_SNAPSHOT_KEY", raising=False)
    monkeypatch.setenv("QUARTZ_SNAPSHOT_KEY_FILE", str(tmp_path / "nonexistent-key"))
    snap_dir = tmp_path / "snap"

    w1 = _make_worker(tmp_path)
    w1._namespace["tup"] = (7, 8)
    w1._namespace["plain"] = [10, 20]
    manifest = w1.snapshot_vars(str(snap_dir))

    # Simulate a legacy unsigned snapshot by removing hmac (and hashes)
    manifest_path = snap_dir / "manifest.json"
    unsigned = dict(manifest)
    unsigned.pop("hmac", None)
    unsigned.pop("hashes", None)
    manifest_path.write_text(json.dumps(unsigned), encoding="utf-8")

    monkeypatch.setenv("QUARTZ_STRICT_SNAPSHOTS", "1")

    def _forbid_unpickle(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unpickler must not be called when QUARTZ_STRICT_SNAPSHOTS=1")

    monkeypatch.setattr(pickle, "loads", _forbid_unpickle)
    monkeypatch.setattr(dill, "loads", _forbid_unpickle)

    w2 = _make_worker(tmp_path)
    res = w2.restore_vars(str(snap_dir))
    assert "tup (unsigned snapshot rejected)" in res["skipped"]
    assert "tup" not in w2._namespace
    assert res["restored"] == ["plain"]
    assert w2._namespace["plain"] == [10, 20]
