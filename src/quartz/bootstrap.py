"""Transport-neutral construction of a live `AgentRuntime`.

`cli/app.py` used to build the runtime inline from an `argparse.Namespace`: it read
`args.*` throughout, printed a red line on a credential problem, and called
`sys.exit(1)` when a resume target was missing. A server can do neither -- it has no
command line, and it must not kill the process because one request named a session
that no longer exists. So the *decisions* live here, expressed as a plain dataclass
and typed exceptions; each transport (terminal, headless, web, ACP) decides how to
report them.

Layer note: this module sits above `core/` (it composes the provider, the kernel and
the session) and below `cli/`, which supplies the argparse adapter. It must not
import `quartz.tui` or `quartz.cli`; `tests/test_bootstrap.py` enforces that, because
importing a front end from here would make the browser transport depend on the
terminal one.

Callers are responsible for process-level environment setup. `cli/app.py` calls
`_load_env_files()` before building a runtime; a web server must do the same (or
supply configuration another way) before calling `build_runtime`.
"""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path
import contextlib
from typing import Any, Callable

from quartz.core.background import resolve_interval
from quartz.core.loop import TurnUsage
from quartz.core.progress import resolve_every
from quartz.core.prompt import build_system_prompt
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session, default_sessions_dir, prune_snapshots
from quartz.errors import SessionNotFoundError
from quartz.kernel.client import KernelClient
from quartz.kernel.sandbox import SandboxConfig
from quartz.providers.registry import resolve_context_tokens
from quartz.providers.selection import build_provider
from quartz.settings import load_settings, trust_project

__all__ = ["BootstrapOptions", "build_runtime", "LazyClient"]


class LazyClient:
    """Delays provider resolution until the first call."""

    def __init__(self, factory: Callable[[], tuple[Any, ...]]) -> None:
        self._factory = factory
        self._resolved: tuple[Any, ...] | None = None
        self._lock = threading.Lock()

    def resolve(self) -> tuple[Any, ...]:
        with self._lock:
            if self._resolved is None:
                self._resolved = self._factory()
            return self._resolved

    @property
    def client(self) -> Any:
        return self.resolve()[0]

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.stream(*args, **kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.complete(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            if self._resolved is not None:
                c = self._resolved[0]
                if hasattr(c, "close"):
                    with contextlib.suppress(Exception):
                        c.close()
                self._resolved = None


@dataclasses.dataclass(frozen=True)
class BootstrapOptions:
    """Everything `build_runtime` needs, with no reference to a command line.

    One field per input the old `_build_runtime(args)` read off the namespace. The
    terminal adapter fills it from argparse; a web server fills it from its own
    request/config handling.
    """

    cwd: Path
    # `None` (or "") means "resolve automatically", exactly like the terminal's
    # `--provider auto` default -- NOT "no provider". A transport that has no
    # provider selector at all (the web server) leaves this alone and gets the
    # same auto-resolution the terminal does; `build_runtime` applies the
    # coercion, so no caller has to remember it. An explicit name still wins.
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    # Maps to `args.prompt`: non-None means headless. It only affects the system
    # prompt and the progress default; it is not "no output".
    headless: bool = False
    resume_id: str | None = None
    fork: bool = False
    cont: bool = False
    verbose: bool = False
    # Whether output is plain text. The terminal derives this from `--prompt` and
    # TTY detection; a web transport simply sets it.
    plain_output: bool = False
    show_thinking: bool = False
    show_details: bool = False
    progress_every: int | None = None
    #: Seconds between status reports for a running background job (``0`` disables).
    #: ``None`` means "use the setting", which is on for an interactive session and
    #: off for ``-p`` -- a headless run has no background jobs to report on.
    background_check_every: int | None = None
    # Which transport is asking. Recorded in the session's `meta` record so the
    # web sidebar can list browser sessions and leave terminal ones alone until
    # they are imported. `core.session.ORIGIN_CLI` for every terminal path.
    origin: str = "cli"
    # Where that transport draws: `"cli"` or `"web"`. Same information as `origin`,
    # kept separate because they answer different questions -- `origin` is a fact
    # about the session on disk and never changes on resume, while this is a fact
    # about who is looking at it now.
    transport: str = "cli"
    trust_project: bool = False
    compact_reserve_tokens: int | None = None
    keep_recent_tokens: int | None = None
    max_context_tokens: int | None = None
    client_factory: Callable[[], tuple[Any, ...]] | None = None
    kernel_factory: Any | None = None
    lazy_provider: bool = False
    sandbox: bool | None = None


def _resolve_sandbox_enabled(explicit: bool | None, setting: bool) -> bool:
    if explicit is not None:
        return explicit
    env_val = os.getenv("QUARTZ_SANDBOX")
    if env_val is not None:
        norm = env_val.strip().lower()
        if norm in {"0", "false", "no", "off"}:
            return False
        if norm in {"1", "true", "yes", "on"}:
            return True
    return setting


def build_runtime(options: BootstrapOptions) -> tuple[AgentRuntime, bool]:
    """Build a live runtime around a started kernel. Returns ``(runtime, resumed)``.

    The kernel starts on a thread *before* the provider is built: building a provider
    resolves model metadata over the network, and the kernel handshake is the other
    slow step, so overlapping them is deliberate latency hiding. Do not serialise it.

    Raises:
        CredentialError: credentials are missing or unusable.
        EndpointConfigError: the configured provider endpoint is contradictory.
        SessionNotFoundError: `cont`/`resume_id` asked for a session and none matches.
        FileNotFoundError: the named session id no longer exists.

    Every one of those leaves with the kernel closed, so a failed build never leaks a
    kernel subprocess. Callers cannot close it themselves: they never see the object.
    """
    cwd = options.cwd.resolve()
    if options.trust_project:
        trust_project(cwd)

    settings = load_settings()
    sandbox_enabled = _resolve_sandbox_enabled(options.sandbox, settings.sandbox)

    kernel_cls = options.kernel_factory or KernelClient
    kernel = kernel_cls(cwd=cwd)
    if sandbox_enabled and hasattr(kernel, "enable_sandbox"):
        kernel.enable_sandbox(SandboxConfig(enabled=True))
    kernel_err: list[BaseException] = []

    def _start_kernel() -> None:
        try:
            kernel.start()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            kernel_err.append(exc)

    kernel_thread = threading.Thread(target=_start_kernel, daemon=True)
    kernel_thread.start()
    try:
        try:
            if options.lazy_provider:
                def _lazy_factory() -> tuple[Any, ...]:
                    if options.client_factory is not None:
                        return options.client_factory()
                    p_client, pname, p_model, p_effort = build_provider(
                        provider=options.provider or "auto",
                        model_override=options.model,
                        effort_override=options.effort,
                    )
                    ctx = resolve_context_tokens(pname, p_model, p_client)
                    return p_client, pname, p_model, p_effort, ctx

                client = LazyClient(_lazy_factory)
                provider_name = ""
                model = ""
                effort = ""
                max_context_tokens = options.max_context_tokens or 128_000
            elif options.client_factory is not None:
                factory_res = options.client_factory()
                if len(factory_res) == 5:
                    client, provider_name, model, effort, resolved_ctx = factory_res
                    max_ctx = (
                        int(resolved_ctx)
                        if resolved_ctx is not None
                        else resolve_context_tokens(provider_name, model, client)
                    )
                else:
                    client, provider_name, model, effort = factory_res[:4]
                    max_ctx = resolve_context_tokens(provider_name, model, client)
                max_context_tokens = options.max_context_tokens or max_ctx
            else:
                client, provider_name, model, effort = build_provider(
                    provider=options.provider or "auto",
                    model_override=options.model,
                    effort_override=options.effort,
                )
                # Resolve from the offline table immediately so startup never blocks
                # on a /models HTTP round-trip; refresh from the provider in the
                # background below once the runtime is built.
                max_ctx = resolve_context_tokens(provider_name, model, client=None)
                max_context_tokens = options.max_context_tokens or max_ctx
            system_prompt = build_system_prompt(cwd, headless=options.headless)
        finally:
            kernel_thread.join()
        # A kernel that failed to start surfaces here rather than on its thread,
        # so the caller sees the original traceback.
        if kernel_err:
            raise kernel_err[0]

        messages: list[dict[str, Any]] = []
        resumed = False
        if options.cont or options.resume_id:
            target_id = options.resume_id or Session.latest_id(cwd=cwd)
            if not target_id:
                raise SessionNotFoundError(cwd)
            if options.fork:
                active_session, messages = Session.fork(target_id, cwd)
            else:
                active_session, messages = Session.load(target_id)
            resumed = True
            restored_notice = active_session.restore_kernel(kernel, root=cwd)
            if restored_notice:
                messages.append({"role": "user", "content": restored_notice})
        else:
            active_session = Session.create(cwd, origin=options.origin)

        # Snapshot pruning is opportunistic housekeeping, not a startup step: it runs
        # after the session resolves and restores so a resumed session's own snapshot
        # is never deleted, and every failure is swallowed so a read-only or vanishing
        # sessions dir can never stop a user from starting work.
        try:
            prune_snapshots(
                default_sessions_dir(),
                older_than_days=settings.snapshot_retention_days,
                exclude_ids={active_session.id},
            )
        except Exception:  # noqa: BLE001 - housekeeping must not break startup
            pass

        background_check_every = resolve_interval(
            options.background_check_every,
            headless=options.headless,
            default=settings.background_check_every,
        )
        # The worker owns the clock, so tell it once here; a restart re-applies it.
        kernel.configure(
            headless=options.headless,
            background_check_every=background_check_every,
        )

        compact_reserve_tokens = (
            options.compact_reserve_tokens
            if options.compact_reserve_tokens is not None
            else settings.compact_reserve_tokens
        )
        keep_recent_tokens = (
            options.keep_recent_tokens
            if options.keep_recent_tokens is not None
            else settings.keep_recent_tokens
        )

        runtime = AgentRuntime(
            cwd=cwd,
            kernel=kernel,
            client=client,
            provider_name=provider_name,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            active_session=active_session,
            messages=messages,
            usage_stats=TurnUsage.from_dict(active_session.last_usage),
            max_context_tokens=max_context_tokens,
            compact_reserve_tokens=compact_reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
            verbose=options.verbose,
            plain_output=options.plain_output,
            headless=options.headless,
            show_thinking=options.show_thinking,
            show_details=options.show_details,
            progress_every=resolve_every(
                options.progress_every,
                headless=options.headless,
                default=settings.progress_every,
            ),
            background_check_every=background_check_every,
            transport=options.transport,
        )
        if (
            not options.lazy_provider
            and options.client_factory is None
            and options.max_context_tokens is None
            and (callable(getattr(client, "list_model_details", None)) or callable(getattr(client, "list_models", None)))
        ):
            def _refresh_context_tokens() -> None:
                try:
                    discovered = resolve_context_tokens(provider_name, model, client)
                    if discovered > 0:
                        runtime.max_context_tokens = discovered
                except Exception:
                    pass

            threading.Thread(target=_refresh_context_tokens, daemon=True).start()
        return runtime, resumed
    except BaseException:
        kernel.close()
        raise
