"""Tests for per-session persistent /tmp and shared cache inside bwrap sandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from quartz.kernel.client import KernelClient
from quartz.kernel.sandbox import SandboxConfig, build_bwrap_command, detect_sandbox_backend


def test_build_bwrap_command_uses_scratch_tmp_and_cache(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    cache = tmp_path / "cache"
    scratch.mkdir()
    cache.mkdir()
    cfg = SandboxConfig(enabled=True, scratch_tmp_dir=scratch, cache_dir=cache)
    argv = build_bwrap_command(["echo", "hi"], tmp_path, cfg, home=tmp_path)

    # /tmp itself must be bind-mounted to scratch, not an ephemeral tmpfs
    tmp_pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == "--tmpfs"]
    assert ("--tmpfs", "/tmp") not in tmp_pairs

    bind_triples = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2) if argv[i] == "--bind"]
    assert ("--bind", str(scratch.resolve()), "/tmp") in bind_triples
    assert ("--bind", str(cache.resolve()), "/tmp/quartz-cache") in bind_triples


@pytest.mark.asyncio
async def test_kernel_client_persists_tmp_within_session_and_isolates_across_sessions(tmp_path: Path) -> None:
    if detect_sandbox_backend() != "bwrap":
        pytest.skip("bwrap not available")

    c1 = KernelClient(cwd=tmp_path, sandbox=True)
    c2 = KernelClient(cwd=tmp_path, sandbox=True)
    try:
        r1 = await c1.exec_cell("Path('/tmp/session_marker.txt').write_text('hello-c1')")
        assert r1.ok, r1.stderr

        # Same session sees /tmp/session_marker.txt across cells
        r2 = await c1.exec_cell("print(Path('/tmp/session_marker.txt').read_text())")
        assert r2.ok, r2.stderr
        assert "hello-c1" in r2.stdout

        # Different session does NOT see c1's /tmp file
        r3 = await c2.exec_cell("print(Path('/tmp/session_marker.txt').exists())")
        assert r3.ok, r3.stderr
        assert "False" in r3.stdout

        scratch_dir = c1._sandbox_config.scratch_tmp_dir
        assert scratch_dir is not None and scratch_dir.is_dir()
    finally:
        scratch_dir = c1._sandbox_config.scratch_tmp_dir
        await c1.aclose()
        await c2.aclose()

    assert scratch_dir is not None and not scratch_dir.exists()
