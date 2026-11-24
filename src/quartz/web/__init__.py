"""The browser transport: a FastAPI server and one WebSocket over the core.

`quartz web` is the browser transport over the same core the terminal drives,
streaming all turn events directly over a dedicated JSON WebSocket protocol --
see `docs/web-protocol.md` for the contract.

Import note: nothing here may import `quartz.tui` or `quartz.cli`. The terminal must
not become a dependency of the browser, and `cli/` is the composition root that
imports *this* when `quartz web` runs. `tests/web/test_import_boundaries.py` enforces
that, mirroring the check `core/` already has.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args: object, **kwargs: object) -> object:
    """Import `quartz.web.app` lazily, so `import quartz.web` stays dependency-free.

    `app.py` pulls in FastAPI and Starlette; a user who only ever runs the terminal
    should not pay for that import, and a machine that installed Quartz without the
    web extras should still get a clear error at the point of use rather than at
    package import.
    """
    from quartz.web.app import create_app as _create_app

    return _create_app(*args, **kwargs)
