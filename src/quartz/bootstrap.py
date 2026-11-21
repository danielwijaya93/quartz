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
import threading
from pathlib import Path
from typing import Any

from quartz.core.loop import TurnUsage
from quartz.core.progress import resolve_every
from quartz.core.prompt import build_system_prompt
from quartz.core.runtime import AgentRuntime
from quartz.core.session import Session, default_sessions_dir, prune_snapshots
from quartz.errors import SessionNotFoundError
from quartz.kernel.client import KernelClient
from quartz.providers.registry import resolve_context_tokens
from quartz.providers.selection import build_provider
from quartz.settings import load_settings

__all__ = ["BootstrapOptions", "build_runtime"]


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

    kernel = KernelClient(cwd=cwd)
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
            # `build_provider` is a library function and rightly rejects junk, so
            # the "unset means auto" decision lives here rather than there: every
            # transport that omits a provider gets auto-resolution for free.
            client, provider_name, model, effort = build_provider(
                provider=options.provider or "auto",
                model_override=options.model,
                effort_override=options.effort,
            )
            max_context_tokens = resolve_context_tokens(provider_name, model, client)
            settings = load_settings()
            system_prompt = build_system_prompt(cwd, headless=options.headless)
        finally:
            kernel_thread.join()
        # A kernel that failed to start surfaces here rather than on its thread,
        # so the caller sees the original traceback.
        if kernel_err:
            raise kernel_err[0]

        # Snapshot pruning is opportunistic housekeeping, not a startup step: it runs
        # after the session resolves so a resumed session's own snapshot is already
        # known-good, and every failure is swallowed so a read-only or vanishing
        # sessions dir can never stop a user from starting work.
        try:
            prune_snapshots(default_sessions_dir(), older_than_days=settings.snapshot_retention_days)
        except Exception:  # noqa: BLE001 - housekeeping must not break startup
            pass

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
            restored_notice = active_session.restore_kernel(kernel)
            if restored_notice:
                messages.append({"role": "user", "content": restored_notice})
        else:
            active_session = Session.create(cwd)

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
            compact_reserve_tokens=settings.compact_reserve_tokens,
            keep_recent_tokens=settings.keep_recent_tokens,
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
        )
        return runtime, resumed
    except BaseException:
        kernel.close()
        raise
