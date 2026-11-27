"""Interactive provider login/logout: the Rich flows behind `/login` and `quartz auth`.

Collecting a credential is a UI job — an OAuth browser handoff, a secret prompt, a
status spinner — and both the slash command and the `auth` subcommand drive the
same flow, so it lives next to the other TUI code instead of in `cli/`. The
non-interactive half (choosing a provider, reporting its status) is
`quartz.providers.selection`.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text

from quartz.auth.google import (
    CALLBACK_PORT,
    auth_file_path,
    delete_google_credentials,
    load_google_credentials,
    login_google_interactive,
)
from quartz.auth.google_ai_plan import login_google_ai_plan_interactive
from quartz.auth.store import (
    delete_google_ai_plan_credentials,
    load_google_ai_plan_credentials,
)
from quartz.providers.registry import (
    PROVIDERS,
    delete_provider_auth,
    discover_models,
    list_logged_in_providers,
    save_provider_auth,
)
from quartz.providers.selection import _provider_status_label
from quartz.tui.modals import _pick_option, _prompt_input
from quartz.tui.render import (
    console,
    console_print,
    dock_status,
    get_active_dock,
)

__all__ = ["_login_provider", "_logout_provider", "show_auth_status"]


def _login_provider(target_provider: str | None = None) -> str | None:
    """Interactive login flow differentiating OAuth vs API-key providers."""
    if target_provider is None:
        opts = [(pname, f"{pspec.label} ({_provider_status_label(pname)})") for pname, pspec in PROVIDERS.items()]
        target_provider = _pick_option("Log in to Provider", opts)
        if not target_provider:
            console.print("[dim]Cancelled.[/dim]")
            return None

    if target_provider not in PROVIDERS:
        console.print(
            f"[red]Unknown provider '{escape(target_provider)}'. "
            f"Available: {', '.join(PROVIDERS.keys())}[/red]"
        )
        return None

    spec = PROVIDERS[target_provider]
    if spec.auth_type == "oauth":
        return _login_oauth_provider(target_provider)

    api_key = _prompt_input(f"{target_provider} API key: ", secret=True)
    if not api_key:
        console.print("[dim]Cancelled.[/dim]")
        return None

    base_url: str | None = spec.base_url or None
    if target_provider == "custom":
        entered_url = _prompt_input("Base URL (e.g. https://api.example.com/v1): ")
        if not entered_url:
            console.print("[dim]Cancelled.[/dim]")
            return None
        base_url = entered_url

    saved_path = save_provider_auth(target_provider, api_key, base_url)
    with dock_status(f"[dim]Verifying {target_provider} & discovering models...[/dim]"):
        models, err = discover_models(target_provider)
    if err:
        console.print(
            f"[yellow]Saved API key for [bold]{escape(target_provider)}[/bold] to {saved_path}, "
            f"but model discovery failed: {escape(err)}[/yellow]"
        )
    else:
        console.print(
            f"[green]Logged in to [bold]{escape(target_provider)}[/bold] (API key)![/green] "
            f"Discovered [bold]{len(models)}[/bold] model(s) (saved to {saved_path})."
        )
    return target_provider


def _logout_provider(target_provider: str | None = None) -> bool:
    """Remove stored credentials for a provider from ~/.quartz/auth.json."""
    logged_in = list_logged_in_providers()
    if target_provider is None:
        if not logged_in:
            console.print(f"[dim]No stored provider logins found in {auth_file_path()}.[/dim]")
            return False
        opts = [
            (pname, f"{PROVIDERS[pname].label if pname in PROVIDERS else pname} ({atype})")
            for pname, atype in logged_in.items()
        ]
        target_provider = _pick_option("Log out from Provider", opts)
        if not target_provider:
            console.print("[dim]Cancelled.[/dim]")
            return False

    if target_provider == "google":
        removed = delete_google_credentials()
    elif target_provider == "google-ai-plan":
        removed = delete_google_ai_plan_credentials()
    else:
        removed = delete_provider_auth(target_provider)

    if removed:
        console.print(f"[green]Logged out from [bold]{escape(target_provider)}[/bold].[/green]")
    else:
        console.print(f"[dim]No stored credentials found for {escape(target_provider)}.[/dim]")
    return removed


def _login_oauth_provider(target_provider: str) -> str | None:
    in_dock = get_active_dock() is not None

    def _on_url(auth_url: str) -> None:
        console_print(Text("Open this URL in your browser to sign in with Google:\n"))
        console_print(Text(f"  {auth_url}\n"))
        if target_provider == "google-ai-plan":
            console_print(Text("Waiting for browser callback on http://127.0.0.1 ..."))
        else:
            console_print(Text(f"Waiting for browser callback on http://localhost:{CALLBACK_PORT} ..."))
        if not in_dock:
            console_print(
                Text("(If on a remote machine, paste the final redirect URL here and press Enter)")
            )

    try:
        if target_provider == "google-ai-plan":
            login_google_ai_plan_interactive(on_url=_on_url, interactive_paste=not in_dock)
            console.print(
                f"[green]Logged in to Google AI Plan (OAuth + DPoP)![/green] (saved to {auth_file_path()})"
            )
            return target_provider
        creds = login_google_interactive(
            on_url=_on_url,
            interactive_paste=not in_dock,
        )
    except Exception as exc:
        console.print(f"[red]Login failed: {escape(str(exc))}[/red]")
        return None
    console.print(
        f"[green]Logged in to Google (OAuth)![/green] Project: [bold]{escape(creds.project_id)}[/bold] "
        f"(saved to {auth_file_path()})"
    )
    return target_provider


def show_auth_status() -> None:
    """Print the OAuth/api-key status of every provider (`quartz auth status`)."""
    creds = load_google_credentials()
    if creds is None:
        console.print(f"[dim]google (oauth): not logged in ({auth_file_path()})[/dim]")
    else:
        status = "expired (will auto-refresh)" if creds.is_expired() else "active"
        console.print(
            f"[green]google (oauth): logged in[/green] | project: [bold]{creds.project_id}[/bold] | token: {status}"
        )
    ai_creds = load_google_ai_plan_credentials()
    if ai_creds is None:
        console.print(f"[dim]google-ai-plan (oauth): not logged in ({auth_file_path()})[/dim]")
    else:
        status = "expired (will auto-refresh)" if ai_creds.is_expired() else "active"
        console.print(f"[green]google-ai-plan (oauth): logged in[/green] | token: {status}")
    for pname, pspec in PROVIDERS.items():
        if pspec.auth_type == "oauth":
            continue
        console.print(f"  [cyan]{pname}[/cyan]: {_provider_status_label(pname)}")
