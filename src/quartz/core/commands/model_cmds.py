"""Model, effort, and auth slash commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quartz.core.commands.registry import (
    _SERVES_WEB_ATTR,
    COMMAND_REGISTRY,
    CommandResult,
    get_transport,
)

if TYPE_CHECKING:
    from quartz.core.runtime import AgentRuntime

logger = logging.getLogger(__name__)

__all__ = ["apply_model_switch", "resolve_model_arg"]


@COMMAND_REGISTRY.register(
    "/login",
    "Log in to a provider (OAuth or API key)",
    usage="[prov]",
    needs_terminal=True,
)
def _cmd_login(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /login in the terminal: it needs an interactive OAuth flow or a hidden API-key prompt."
    )


@COMMAND_REGISTRY.register(
    "/logout",
    "Log out and remove stored provider credentials",
    usage="[prov]",
    needs_terminal=True,
)
def _cmd_logout(rt: AgentRuntime, arg: str) -> CommandResult | None:
    del rt, arg
    return CommandResult.needs_terminal(
        "Run /logout in the terminal: it edits the credential store you are logged in from."
    )


def resolve_model_arg(arg: str, current_provider: str) -> tuple[str, str | None, str | None]:
    """Parse a user's `/model` argument into (provider, model, effort)."""
    parts = arg.strip().split()
    if not parts:
        return current_provider, None, None

    from quartz.providers.registry import PROVIDERS
    from quartz.providers.selection import _provider_ready

    all_efforts = {"low", "medium", "high", "max", "xhigh", "minimal"}

    if parts[0].lower() in PROVIDERS:
        provider = parts[0].lower()
        rest = parts[1:]
        if not rest:
            return provider, None, None
        if len(rest) >= 2 and rest[-1].lower() in all_efforts:
            return provider, " ".join(rest[:-1]), rest[-1].lower()
        return provider, " ".join(rest), None

    if len(parts) >= 2 and parts[-1].lower() in all_efforts:
        model_part = " ".join(parts[:-1])
        effort = parts[-1].lower()
    else:
        model_part = " ".join(parts)
        effort = None

    if "/" in model_part:
        prefix, remainder = model_part.split("/", 1)
        if prefix.lower() in PROVIDERS:
            return prefix.lower(), remainder, effort

    if model_part.startswith("gemini-"):
        return "google", model_part, effort
    if model_part.startswith(("gpt-", "o1", "o3", "o4")):
        if _provider_ready("openai"):
            return "openai", model_part, effort
        if _provider_ready("openrouter"):
            return "openrouter", model_part, effort
        return "openai", model_part, effort
    if model_part.startswith("deepseek-"):
        if _provider_ready("deepseek"):
            return "deepseek", model_part, effort
        if _provider_ready("openrouter"):
            return "openrouter", model_part, effort
        return "deepseek", model_part, effort
    if "/" in model_part:
        return "openrouter", model_part, effort

    return current_provider, model_part, effort


def apply_model_switch(
    rt: AgentRuntime,
    provider: str,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[bool, str]:
    """Switch the runtime to a new provider/model/effort, persisting to config."""
    from quartz.errors import CredentialError, EndpointConfigError
    from quartz.providers.registry import resolve_context_tokens, save_model_config
    from quartz.providers.selection import build_provider

    old_client = rt.client
    try:
        new_client, pname, new_model, new_effort = build_provider(
            provider=provider,
            model_override=model,
            effort_override=effort,
        )
    except (CredentialError, EndpointConfigError, ValueError) as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Failed to switch model: {exc}"

    rt.client = new_client
    rt.provider_name = pname
    rt.model = new_model
    rt.effort = new_effort

    if old_client is not None and old_client is not rt.client:
        close_fn = getattr(old_client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                logger.debug("Failed to close previous client", exc_info=True)

    try:
        save_model_config(rt.provider_name, rt.model, rt.effort)
    except Exception:
        logger.debug("Failed to save model config", exc_info=True)

    try:
        rt.max_context_tokens = resolve_context_tokens(rt.provider_name, rt.model, rt.client)
    except Exception:
        logger.debug("Failed to resolve context tokens", exc_info=True)

    return True, f"Switched to {rt.provider_name} / {rt.model} (effort: {rt.effort})."


@COMMAND_REGISTRY.register(
    "/model",
    "Choose provider, model, and reasoning effort",
    usage="[model] [effort]",
    needs_terminal=True,
)
def _cmd_model(rt: AgentRuntime, arg: str) -> CommandResult | None:
    trimmed = arg.strip()
    if trimmed:
        provider, model, effort = resolve_model_arg(trimmed, rt.provider_name)
        ok, msg = apply_model_switch(rt, provider, model, effort)
        if not ok:
            return CommandResult.error(f"Failed to switch model: {msg}")
        return CommandResult.text(msg, title="/model")

    if get_transport(rt) == "web":
        return CommandResult.text(
            f"Active model: **{rt.model}** ({rt.provider_name}, effort: {rt.effort}).\n\n"
            f"Use the model picker in the header or run `/model <name> [effort]` to switch.",
            title="/model",
        )
    return CommandResult.needs_terminal(
        "Run /model in the terminal to pick provider, model, and reasoning effort."
    )


setattr(_cmd_model, _SERVES_WEB_ATTR, True)
