"""Red-first tests for Pillar #1 PR 1: sandbox masking, env allowlist, and server-side bridge validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quartz.kernel.bridge import validate_bridge_command
from quartz.kernel.client import KernelClient
from quartz.kernel.host_rpc import HostRpcHandler
from quartz.kernel.sandbox import (
    DEFAULT_DENY_WRITE,
    SandboxConfig,
    build_bwrap_command,
    build_sandbox_env,
    build_seatbelt_profile,
)


class TestSandboxFilesystemHardening:
    def test_bwrap_masks_quartz_dir_and_allows_non_secret_files_ro(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        (cwd / ".quartz").mkdir(parents=True)
        (cwd / ".quartz" / "mcp.json").write_text("{}", encoding="utf-8")
        (cwd / ".quartz" / "helpers.py").write_text("", encoding="utf-8")

        home = tmp_path / "home"
        qdir = home / ".quartz"
        qdir.mkdir(parents=True)
        (qdir / "web-token").write_text("super-secret-token", encoding="utf-8")
        (qdir / "snapshot-key").write_text("deadbeef", encoding="utf-8")
        (qdir / "quartz.db").write_text("sqlite", encoding="utf-8")
        (qdir / "auth.json").write_text("{}", encoding="utf-8")
        (qdir / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        (qdir / "helpers.py").write_text("def hi(): pass\n", encoding="utf-8")
        (qdir / "skills").mkdir()
        (home / ".gnupg").mkdir()
        (home / ".docker").mkdir()
        (home / ".netrc").write_text("machine example password secret", encoding="utf-8")
        (home / ".git-credentials").write_text("https://user:pass@github.com", encoding="utf-8")

        wrapped = build_bwrap_command(
            ["python", "-u", "-m", "quartz.kernel.worker"],
            cwd,
            SandboxConfig(enabled=True),
            home=home,
            bwrap_bin="/usr/bin/bwrap",
        )

        # Entire ~/.quartz, ~/.gnupg, ~/.docker masked with tmpfs
        for d in (qdir, home / ".gnupg", home / ".docker"):
            p = str(d.resolve())
            assert any(wrapped[i : i + 2] == ["--tmpfs", p] for i in range(len(wrapped) - 1)), p

        # Non-secret ~/.quartz files selectively ro-bound AFTER --tmpfs ~/.quartz
        tmpfs_q_idx = next(
            i for i in range(len(wrapped) - 1) if wrapped[i : i + 2] == ["--tmpfs", str(qdir.resolve())]
        )
        for safe_name in ("AGENTS.md", "helpers.py", "skills"):
            sp = str((qdir / safe_name).resolve())
            ro_indices = [
                i for i in range(len(wrapped) - 2) if wrapped[i : i + 3] == ["--ro-bind", sp, sp]
            ]
            assert ro_indices and ro_indices[0] > tmpfs_q_idx, safe_name

        # Credential files masked with /dev/null
        for cred in (home / ".netrc", home / ".git-credentials"):
            cp = str(cred.resolve())
            assert any(
                wrapped[i : i + 3] == ["--ro-bind", "/dev/null", cp]
                for i in range(len(wrapped) - 2)
            ), cp

        # Project .quartz/mcp.json & .quartz/helpers.py denied write
        assert ".quartz/mcp.json" in DEFAULT_DENY_WRITE
        assert ".quartz/helpers.py" in DEFAULT_DENY_WRITE
        for rel in (".quartz/mcp.json", ".quartz/helpers.py"):
            tp = str((cwd / rel).resolve())
            assert any(
                wrapped[i : i + 3] == ["--ro-bind", tp, tp] for i in range(len(wrapped) - 2)
            ), rel

    def test_seatbelt_profile_masks_quartz_and_allows_non_secret_reads(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        home = tmp_path / "home"
        qdir = home / ".quartz"
        qdir.mkdir(parents=True)
        (qdir / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")

        profile = build_seatbelt_profile(cwd, SandboxConfig(enabled=True), home=home)
        deny_line = f'(deny file-read* file-write* (subpath "{qdir.resolve()}"))'
        allow_line = f'(allow file-read* (literal "{(qdir / "AGENTS.md").resolve()}"))'
        assert deny_line in profile
        assert allow_line in profile
        # Seatbelt uses last-match-wins: allow must appear AFTER deny
        assert profile.index(allow_line) > profile.index(deny_line)

    def test_bwrap_never_masks_cwd_when_cwd_inside_quartz_dir(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        cwd = home / ".quartz" / "skills"
        cwd.mkdir(parents=True)
        wrapped = build_bwrap_command(["python"], cwd, SandboxConfig(enabled=True), home=home)
        qdir = str((home / ".quartz").resolve())
        assert not any(
            wrapped[i : i + 2] == ["--tmpfs", qdir] for i in range(len(wrapped) - 1)
        )


class TestSandboxEnvScrubbing:
    def test_build_sandbox_env_strips_secrets_and_keeps_safe_vars(self) -> None:
        host_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/alice",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C.UTF-8",
            "VIRTUAL_ENV": "/app/.venv",
            "QUARTZ_WEB_TOKEN": "super-secret-web-token",
            "QUARTZ_SNAPSHOT_KEY": "deadbeef" * 8,
            "OPENAI_API_KEY": "sk-secret",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "GOOGLE_API_KEY": "AIza-secret",
            "GH_TOKEN": "ghp_secret",
            "GITHUB_TOKEN": "ghp_secret2",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "RANDOM_SECRET_VAR": "should-not-leak",
        }
        scrubbed = build_sandbox_env(host_env)
        assert scrubbed["PATH"] == "/usr/bin:/bin"
        assert scrubbed["HOME"] == "/home/alice"
        assert scrubbed["LANG"] == "en_US.UTF-8"
        assert scrubbed["LC_ALL"] == "C.UTF-8"
        assert scrubbed["VIRTUAL_ENV"] == "/app/.venv"
        for secret_key in (
            "QUARTZ_WEB_TOKEN",
            "QUARTZ_SNAPSHOT_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "RANDOM_SECRET_VAR",
        ):
            assert secret_key not in scrubbed

    def test_passthrough_allows_safe_opt_in_but_never_quartz_host_secrets(self) -> None:
        host_env = {
            "PATH": "/usr/bin",
            "MY_APP_ENV": "staging",
            "QUARTZ_WEB_TOKEN": "secret-token",
            "QUARTZ_SNAPSHOT_KEY": "secret-hmac",
            "QUARTZ_SANDBOX_ENV_PASSTHROUGH": "MY_APP_ENV,QUARTZ_WEB_TOKEN,QUARTZ_SNAPSHOT_KEY",
        }
        scrubbed = build_sandbox_env(host_env)
        assert scrubbed["MY_APP_ENV"] == "staging"
        assert "QUARTZ_WEB_TOKEN" not in scrubbed
        assert "QUARTZ_SNAPSHOT_KEY" not in scrubbed

    def test_kernel_client_scrubs_env_when_sandbox_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUARTZ_WEB_TOKEN", "leaked-token")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-leaked")
        monkeypatch.setattr("quartz.kernel.sandbox.detect_sandbox_backend", lambda **_kw: "bwrap")
        monkeypatch.setattr("quartz.kernel.sandbox.shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

        kc_sb = KernelClient(cwd=tmp_path, sandbox=True)
        _cmd, sb_env = kc_sb._build_spawn_cmd_and_env()
        assert "QUARTZ_WEB_TOKEN" not in sb_env
        assert "OPENAI_API_KEY" not in sb_env
        assert sb_env.get("QUARTZ_SANDBOXED") == "1"

        # Unsandboxed mode preserves host env explicitly
        kc_off = KernelClient(cwd=tmp_path, sandbox=False)
        _cmd2, off_env = kc_off._build_spawn_cmd_and_env()
        assert off_env.get("OPENAI_API_KEY") == os.environ["OPENAI_API_KEY"]


class TestHostBridgeServerSideValidation:
    @pytest.mark.parametrize(
        ("tool", "argv"),
        [
            ("git", ["push", "-u", "origin", "feat/x"]),
            ("git", ["pull", "--rebase", "origin", "main"]),
            ("git", ["fetch", "origin"]),
            ("git", ["ls-remote", "origin"]),
            ("git", ["clone", "https://github.com/org/repo.git"]),
            ("gh", ["pr", "create", "--title", "hi", "--body", "ok"]),
            ("gh", ["pr", "checks", "214", "--watch"]),
            ("gh", ["pr", "merge", "214", "--squash", "--delete-branch"]),
            ("gh", ["api", "repos/org/repo/issues"]),
            ("gh", ["--help"]),
            ("gh", ["--version"]),
            ("quartz", ["-p", "do task", "--cwd", ".", "--model", "gemini-4-hydrogen"]),
        ],
    )
    def test_safe_bridge_commands_are_allowed(self, tool: str, argv: list[str]) -> None:
        assert validate_bridge_command(tool, argv) is None

    @pytest.mark.parametrize(
        ("tool", "argv"),
        [
            # Git flag & protocol RCE vectors
            ("git", ["-c", "core.sshCommand=id", "push"]),
            ("git", ["-ccore.sshCommand=id", "push"]),
            ("git", ["-C", "evil_repo", "push"]),
            ("git", ["-Cevil_repo", "push"]),
            ("git", ["clone", "--config", "core.sshCommand=id", "https://example.com/r.git"]),
            ("git", ["clone", "--config=core.sshCommand=id", "https://example.com/r.git"]),
            ("git", ["--config-env=core.sshCommand=X", "push"]),
            ("git", ["--exec-path=/tmp/evil", "fetch"]),
            ("git", ["--git-dir=/tmp/evil", "push"]),
            ("git", ["fetch", "--upload-pack=sh -c id", "origin"]),
            ("git", ["clone", "-u", "sh", "https://example.com/r.git"]),
            ("git", ["push", "--receive-pack=sh", "origin"]),
            ("git", ["clone", "ext::sh -c id% /tmp/r"]),
            ("git", ["status"]),
            ("git", ["config", "--global", "alias.x", "!sh"]),
            # Quartz privilege-escalation vectors
            ("quartz", ["-p", "hi", "--sandbox-off"]),
            ("quartz", ["-p", "hi", "--no-sandbox"]),
            ("quartz", ["-p", "hi", "--trust"]),
            ("quartz", ["web", "--port", "9999"]),
            ("quartz", ["auth", "login", "openai"]),
            ("quartz", ["mcp", "add", "evil", "--", "sh"]),
            # gh token exfiltration & host RCE vectors
            ("gh", ["auth", "token"]),
            ("gh", ["auth", "login"]),
            ("gh", ["auth", "--hostname", "github.com", "token"]),
            ("gh", ["-R", "owner/repo", "auth", "token"]),
            ("gh", ["--hostname", "github.com", "extension", "install", "evil/ext"]),
            ("gh", ["--repo", "owner/repo", "config", "set", "editor", "evil"]),
            ("gh", ["extension", "install", "evil/ext"]),
            ("gh", ["alias", "set", "pwn", "!sh"]),
            ("gh", ["config", "set", "editor", "sh"]),
            ("gh", ["ssh-key", "add", "id_rsa.pub"]),
        ],
    )
    def test_dangerous_bridge_commands_rejected_before_prompt_or_spawn(
        self, tmp_path: Path, tool: str, argv: list[str]
    ) -> None:
        err = validate_bridge_command(tool, argv)
        assert err is not None, f"Expected {tool} {argv} to be rejected"

        prompted = False

        def _should_not_prompt(_req: object) -> str:
            nonlocal prompted
            prompted = True
            return "allow_once"

        handler = HostRpcHandler(
            cwd=tmp_path,
            permission_handler=_should_not_prompt,  # type: ignore[arg-type]
            preapproved_scopes={"git_remote", "gh", "quartz_worker"},
        )
        res = handler.exec_bridge_sync({"tool": tool, "argv": argv, "cwd": str(tmp_path)})
        assert res["exit_code"] == 126
        assert prompted is False

    def test_nested_git_subdir_rejected_before_prompt_or_spawn(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        evil_sub = tmp_path / "sub"
        (evil_sub / ".git").mkdir(parents=True)
        (evil_sub / ".git" / "config").write_text("[core]\n\tsshCommand = id\n", encoding="utf-8")
        deep_sub = evil_sub / "deep"
        deep_sub.mkdir()
        normal_sub = tmp_path / "normal_sub"
        normal_sub.mkdir()

        prompted = False

        def _track_prompt(_req: object) -> str:
            nonlocal prompted
            prompted = True
            return "reject"

        handler = HostRpcHandler(
            cwd=tmp_path,
            permission_handler=_track_prompt,  # type: ignore[arg-type]
            preapproved_scopes={"git_remote"},
        )
        for bad_dir in (evil_sub, deep_sub):
            res = handler.exec_bridge_sync({"tool": "git", "argv": ["push"], "cwd": str(bad_dir)})
            assert res["exit_code"] == 126
            assert "nested .git" in res["stderr"]
            assert prompted is False

        # Normal subdirectory inheriting root .git is not rejected by nested-.git check
        handler_reject = HostRpcHandler(cwd=tmp_path, permission_handler=_track_prompt)  # type: ignore[arg-type]
        res_normal = handler_reject.exec_bridge_sync(
            {"tool": "git", "argv": ["push"], "cwd": str(normal_sub)}
        )
        assert res_normal["exit_code"] == 126
        assert "Permission rejected" in res_normal["stderr"]
        assert prompted is True


async def test_real_bwrap_hides_quartz_secrets_and_scrubs_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _bwrap_helpers import bwrap_unavailable, require_bwrap

    require_bwrap()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("QUARTZ_WEB_TOKEN", "bwrap-secret-token-xyz")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bwrap-secret-xyz")

    kernel = KernelClient(cwd=workspace, sandbox=True)
    try:
        kernel.start(timeout=15.0)
    except Exception as exc:  # pragma: no cover - restricted userns
        bwrap_unavailable(f"bwrap cannot start in this environment: {exc}")

    try:
        res = await kernel.exec_cell(
            "import os\n"
            "from pathlib import Path\n"
            "assert 'QUARTZ_WEB_TOKEN' not in os.environ\n"
            "assert 'OPENAI_API_KEY' not in os.environ\n"
            "assert not (Path.home() / '.quartz' / 'web-token').exists()\n"
            "assert not (Path.home() / '.quartz' / 'snapshot-key').exists()\n"
            "assert not (Path.home() / '.quartz' / 'quartz.db').exists()\n"
            "'sealed'"
        )
        assert res.ok, res.stderr
        assert res.result == "'sealed'"
    finally:
        kernel.close()
