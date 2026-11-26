"""Structural modularity tests enforcing DE-bar file (<=500 LOC) and function (<=80 LOC) budgets."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DECOMPOSED_MODULES = (
    "src/quartz/kernel/client.py",
    "src/quartz/kernel/client_transport.py",
    "src/quartz/providers/google.py",
    "src/quartz/providers/google_catalog.py",
    "src/quartz/providers/google_codec.py",
    "src/quartz/tui/commands.py",
    "src/quartz/tui/command_render.py",
    "src/quartz/tui/command_modals.py",
    "src/quartz/core/loop.py",
    "src/quartz/core/loop_model.py",
    "src/quartz/web/routes/settings.py",
    "src/quartz/web/routes/settings_ops.py",
    "src/quartz/web/routes/bots.py",
    "src/quartz/web/routes/bots_helpers.py",
)


def test_decomposed_modules_exist_and_stay_under_500_lines() -> None:
    missing: list[str] = []
    oversized: list[str] = []
    for rel in DECOMPOSED_MODULES:
        path = ROOT / rel
        if not path.is_file():
            missing.append(rel)
            continue
        loc = len(path.read_text(encoding="utf-8").splitlines())
        if loc > 500:
            oversized.append(f"{rel} ({loc} LOC)")
    assert missing == [], f"Missing extracted modules: {missing}"
    assert oversized == [], f"Modules exceeding 500 LOC budget: {oversized}"


def test_decomposed_module_functions_stay_under_80_lines() -> None:
    offenders: list[str] = []
    for rel in DECOMPOSED_MODULES:
        path = ROOT / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                if span > 80:
                    offenders.append(f"{rel}:{node.lineno} {node.name}() ({span} LOC)")
    assert offenders == [], f"Functions exceeding 80 LOC budget: {offenders}"


def test_backward_compatible_reexports_preserved() -> None:
    import quartz.core.loop as loop_mod
    import quartz.kernel.client as client_mod
    import quartz.providers.google as google_mod
    import quartz.tui.commands as tui_cmds_mod
    import quartz.web.routes.bots as bots_mod
    import quartz.web.routes.settings as settings_mod

    for name in ("KernelClient", "_AsyncProcHandle", "_HOST_IMAGE_SINK", "_abort_bridge_proc", "_bridge_err"):
        assert hasattr(client_mod, name), f"quartz.kernel.client missing {name}"

    for name in (
        "GoogleAntigravityProvider",
        "build_cloudcode_request",
        "parse_sse_payload",
        "discover_antigravity_models",
        "resolve_antigravity_runtime",
        "get_thinking_config",
        "get_max_output_tokens",
        "clear_antigravity_catalog_cache",
        "is_selectable_runtime_id",
        "parse_model_suffix",
    ):
        assert hasattr(google_mod, name), f"quartz.providers.google missing {name}"

    for name in (
        "render_command_result",
        "_handle_slash_command",
        "_handle_slash_command_async",
        "_terminal_model",
        "_terminal_settings",
        "_terminal_jobs",
        "_terminal_mcp",
    ):
        assert hasattr(tui_cmds_mod, name), f"quartz.tui.commands missing {name}"

    for name in (
        "TurnAborted",
        "TurnUsage",
        "TOOLS",
        "drain_bash_events",
        "_completion_to_dict",
        "_call_model",
        "_call_model_once",
        "agent_loop",
    ):
        assert hasattr(loop_mod, name), f"quartz.core.loop missing {name}"

    for name in (
        "create_router",
        "_settings_values",
        "_mcp_snapshot",
        "_build_mcp_spec",
        "_collect_session_jobs",
        "_load_provider_descriptor",
    ):
        assert hasattr(settings_mod, name), f"quartz.web.routes.settings missing {name}"

    for name in (
        "create_router",
        "_serialize_bot",
        "_generate_bot_id",
        "_config_from_create",
        "_merge_update",
        "_verify_github_signature",
    ):
        assert hasattr(bots_mod, name), f"quartz.web.routes.bots missing {name}"
