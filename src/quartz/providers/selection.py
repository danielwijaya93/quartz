"""Which provider to talk to, and whether its credentials are in place.

`build_provider` is the single place that turns "auto" (or an explicit provider)
plus the saved config, environment and stored credentials into a live
`LLMProvider`; the two status helpers describe what `/login` and `quartz auth
status` show next to each provider. The interactive flows that *collect* those
credentials are UI and live in `quartz.tui.provider_setup`; the CLI subcommand and
the TUI both build their clients from here.
"""

from __future__ import annotations

import os

from quartz.auth.google import load_google_credentials
from quartz.auth.google_ai_plan import load_google_ai_plan_credentials
from quartz.errors import CredentialError, EndpointConfigError
from quartz.providers.base import LLMProvider
from quartz.providers.google import GoogleAntigravityProvider
from quartz.providers.google_interactions import GoogleInteractionsProvider
from quartz.providers.registry import (
    PROVIDERS,
    list_logged_in_providers,
    load_model_config,
    resolve_effort,
    resolve_provider_credentials,
)

__all__ = ["_provider_ready", "_provider_status_label", "build_provider"]


def _provider_ready(provider: str) -> bool:
    pspec = PROVIDERS.get(provider)
    if not pspec:
        return False
    if pspec.auth_type == "oauth":
        if provider == "google-ai-plan":
            return load_google_ai_plan_credentials() is not None
        return load_google_credentials() is not None
    api_key, _, _ = resolve_provider_credentials(provider)
    return bool(api_key)


def _provider_status_label(provider: str) -> str:
    pspec = PROVIDERS[provider]
    if pspec.auth_type == "oauth":
        logged_in = (
            load_google_ai_plan_credentials() is not None
            if provider == "google-ai-plan"
            else load_google_credentials() is not None
        )
        return "logged in • oauth" if logged_in else "not logged in • oauth"
    _, _, source = resolve_provider_credentials(provider)
    if source == "auth.json":
        return "logged in • api key"
    return "not configured • api key"


def build_provider(
    provider: str = "auto",
    model_override: str | None = None,
    effort_override: str | None = None,
    *,
    shared_http: bool = False,
) -> tuple[LLMProvider, str, str, str]:
    """Instantiate an LLMProvider, returning (client, provider_name, model, effort).

    Raises EndpointConfigError for unknown providers and CredentialError when
    required credentials are missing.
    """
    has_google = load_google_credentials() is not None
    has_google_ai_plan = load_google_ai_plan_credentials() is not None
    logged_in = list_logged_in_providers()
    saved = load_model_config()
    env_model = model_override or os.getenv("MODEL")

    if provider == "auto":
        saved_provider = saved.get("provider")
        if saved_provider in PROVIDERS and _provider_ready(saved_provider):
            chosen_provider = saved_provider
        elif env_model and env_model.startswith("gemini-") and has_google_ai_plan:
            chosen_provider = "google-ai-plan"
        elif env_model and env_model.startswith("gemini-") and has_google:
            chosen_provider = "google"
        elif any(p in PROVIDERS and p not in ("google", "google-ai-plan") for p in logged_in):
            chosen_provider = next(
                p for p in PROVIDERS if p not in ("google", "google-ai-plan") and p in logged_in
            )
        elif has_google_ai_plan:
            chosen_provider = "google-ai-plan"
        elif has_google:
            chosen_provider = "google"
        else:
            chosen_provider = "deepseek"
    elif provider in PROVIDERS:
        chosen_provider = provider
    else:
        raise EndpointConfigError(f"Unknown provider '{provider}'.")

    spec = PROVIDERS[chosen_provider]
    if model_override:
        model = model_override
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("model"):
        model = saved["model"]
    elif env_model and (spec.backend == "openai" or env_model in spec.models):
        model = env_model
    else:
        model = spec.default_model

    # Effort: explicit override / saved / env wins when the (discovered) model
    # supports it; otherwise fall back to the model's own default. Levels differ
    # per model (e.g. OpenRouter exposes max/xhigh/minimal on some models only).
    requested_effort: str | None
    effort_source: str
    if effort_override:
        requested_effort, effort_source = effort_override, "--effort"
    elif provider == "auto" and saved.get("provider") == chosen_provider and saved.get("effort"):
        requested_effort, effort_source = saved["effort"], "saved config"
    elif os.getenv("EFFORT"):
        requested_effort, effort_source = os.getenv("EFFORT"), "EFFORT env"
    else:
        requested_effort, effort_source = None, "default"
    effort = resolve_effort(
        chosen_provider,
        model,
        override=requested_effort,
        override_source=effort_source,
    )

    if spec.backend == "google":
        if not has_google:
            raise CredentialError(
                "Not logged in to Google. Run `/login google` or `quartz auth login google` first."
            )
        return GoogleAntigravityProvider(
            shared_http=shared_http,
        ), chosen_provider, model, effort

    if spec.backend == "google_interactions":
        if not has_google_ai_plan:
            raise CredentialError(
                "Not logged in to Google AI Plan. Run `/login google-ai-plan` or `quartz auth login google-ai-plan` first."
            )
        return GoogleInteractionsProvider(
            shared_http=shared_http,
        ), chosen_provider, model, effort

    api_key, base_url, _ = resolve_provider_credentials(chosen_provider)
    if not api_key:
        raise CredentialError(
            f"No credentials found for '{chosen_provider}'. "
            f"Run `/login {chosen_provider}` or `quartz auth login {chosen_provider}`."
        )
    from quartz.providers.openai import OpenAIProvider

    client = OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        shared_http=shared_http,
    )
    return client, chosen_provider, model, effort
