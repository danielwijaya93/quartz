"""The kernel lands where the session says, not where an old snapshot says.

A snapshot records the directory the kernel was in when it was saved, because a
person who ``os.chdir``-ed expects to come back. That absolute path is only
meaningful while it names a place inside the folder the session is being resumed
in: a session reopened from elsewhere -- or one whose snapshot predates the folder
being corrected -- would otherwise restore the kernel into a project the rest of
the runtime does not believe it is in, and the model then reads the wrong files.
"""

from __future__ import annotations

import json
from pathlib import Path

from quartz.kernel.worker import KernelWorker


def _restore_cwd(saved: object, root: str | None) -> str | None:
    return KernelWorker._restore_cwd(saved, root)


def test_no_root_keeps_the_snapshot_directory(tmp_path: Path) -> None:
    """A caller that did not say where it is has said nothing better than the snapshot."""
    assert _restore_cwd(str(tmp_path), None) == str(tmp_path)


def test_snapshot_inside_the_root_is_honoured(tmp_path: Path) -> None:
    """A subdirectory the person walked into is theirs to keep."""
    sub = tmp_path / "pkg"
    sub.mkdir()
    assert _restore_cwd(str(sub), str(tmp_path)) == str(sub)


def test_root_itself_is_honoured(tmp_path: Path) -> None:
    assert _restore_cwd(str(tmp_path), str(tmp_path)) == str(tmp_path)


def test_snapshot_outside_the_root_is_ignored(tmp_path: Path) -> None:
    """The bug: a snapshot saved in another project must not drag the kernel there."""
    other = tmp_path / "other"
    root = tmp_path / "root"
    other.mkdir()
    root.mkdir()
    assert _restore_cwd(str(other), str(root)) == str(root)


def test_an_ancestor_of_the_root_is_ignored(tmp_path: Path) -> None:
    """A snapshot taken in a *parent* folder predates the session's folder being known."""
    root = tmp_path / "root"
    root.mkdir()
    assert _restore_cwd(str(tmp_path), str(root)) == str(root)


def test_a_sibling_sharing_a_prefix_is_ignored(tmp_path: Path) -> None:
    """`/a/b-extra` is not inside `/a/b`, however the strings start."""
    root = tmp_path / "b"
    sibling = tmp_path / "b-extra"
    root.mkdir()
    sibling.mkdir()
    assert _restore_cwd(str(sibling), str(root)) == str(root)


def test_a_vanished_snapshot_directory_falls_back_to_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert _restore_cwd(str(tmp_path / "gone"), str(root)) == str(root)


def test_a_vanished_root_yields_no_move(tmp_path: Path) -> None:
    """Nothing usable to move to: stay put rather than chdir into a missing path."""
    assert _restore_cwd(str(tmp_path), str(tmp_path / "gone")) is None


def test_no_snapshot_cwd_still_moves_to_a_usable_root(tmp_path: Path) -> None:
    """An older manifest has no `cwd`; the root is still the right place to be."""
    (tmp_path / "root").mkdir()
    assert _restore_cwd(None, str(tmp_path / "root")) == str(tmp_path / "root")


def test_a_non_string_snapshot_cwd_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert _restore_cwd(123, str(root)) == str(root)


def test_restore_vars_applies_the_root(tmp_path: Path, monkeypatch) -> None:
    """`restore_vars` must move the process, not merely compute a path."""
    import os

    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(
        json.dumps({"saved": [], "skipped": [], "modules": {}, "files": {}, "cwd": str(other)}),
        encoding="utf-8",
    )

    worker = object.__new__(KernelWorker)
    worker._namespace = {}
    worker._default_bindings = {}
    before = os.getcwd()
    try:
        worker.restore_vars(str(snapshot), root=str(root))
        assert Path(os.getcwd()).resolve() == root.resolve()
    finally:
        os.chdir(before)
