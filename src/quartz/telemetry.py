"""Correlation IDs, append-only SQLite security audit log, and Prometheus-ready metrics."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

from quartz.storage import QuartzDB, get_shared_db

logger = logging.getLogger(__name__)

__all__ = [
    "AuditLogger",
    "MetricsRegistry",
    "correlation_context",
    "get_audit_logger",
    "get_correlation_ids",
    "get_metrics_registry",
]

_CORRELATION_IDS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "quartz_correlation_ids",
    default=None,
)


def get_correlation_ids() -> dict[str, str]:
    """Return a shallow copy of the active correlation IDs for the current async/thread task."""
    return dict(_CORRELATION_IDS.get() or {})


@contextlib.contextmanager
def correlation_context(
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    bot_id: str | None = None,
    run_id: str | None = None,
) -> Iterator[dict[str, str]]:
    """Bind correlation IDs for the current context; nested scopes merge and restore cleanly."""
    merged = dict(_CORRELATION_IDS.get() or {})
    for key, val in (
        ("session_id", session_id),
        ("turn_id", turn_id),
        ("bot_id", bot_id),
        ("run_id", run_id),
    ):
        if val is not None and str(val).strip():
            merged[key] = str(val).strip()
    token = _CORRELATION_IDS.set(merged)
    try:
        yield merged
    finally:
        _CORRELATION_IDS.reset(token)


class AuditLogger:
    """Append-only security & operational audit logger backed by SQLite ``audit_log``."""

    def __init__(self, db: QuartzDB | None = None) -> None:
        self._db = db

    def _get_db(self) -> QuartzDB:
        return self._db if self._db is not None else get_shared_db()

    def record(
        self,
        event_type: str,
        *,
        actor: str = "system",
        decision: str = "",
        summary: str = "",
        details: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        bot_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Append one audit event; never raises so broken storage cannot break execution."""
        corr = _CORRELATION_IDS.get() or {}
        eff_session = session_id if session_id is not None else corr.get("session_id")
        eff_bot = bot_id if bot_id is not None else corr.get("bot_id")
        eff_turn = turn_id if turn_id is not None else corr.get("turn_id")
        eff_run = run_id if run_id is not None else corr.get("run_id")
        now = float(ts) if ts is not None else time.time()
        details_json = json.dumps(dict(details) if details is not None else {}, default=str)
        try:
            with self._get_db().write_transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log (
                        ts, event_type, actor, session_id, bot_id, turn_id, run_id,
                        decision, summary, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        event_type,
                        actor,
                        eff_session,
                        eff_bot,
                        eff_turn,
                        eff_run,
                        decision,
                        summary,
                        details_json,
                    ),
                )
        except Exception:
            logger.debug("Failed to append audit_log event %s", event_type, exc_info=True)

    def query(
        self,
        *,
        event_type: str | None = None,
        bot_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query recent audit events newest-first with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if bot_id:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded = max(1, min(int(limit), 500))
        params.append(bounded)
        rows = (
            self._get_db()
            .read_connection()
            .execute(
                f"""
                SELECT id, ts, event_type, actor, session_id, bot_id, turn_id, run_id,
                       decision, summary, details_json
                FROM audit_log
                {where}
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            )
            .fetchall()
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed_details = json.loads(row[10]) if row[10] else {}
            except ValueError:
                parsed_details = {}
            out.append(
                {
                    "id": int(row[0]),
                    "ts": float(row[1]),
                    "eventType": str(row[2]),
                    "actor": str(row[3]),
                    "sessionId": row[4],
                    "botId": row[5],
                    "turnId": row[6],
                    "runId": row[7],
                    "decision": str(row[8]),
                    "summary": str(row[9]),
                    "details": parsed_details if isinstance(parsed_details, dict) else {},
                }
            )
        return out


def _format_metric_key(name: str, labels: Mapping[str, str] | None) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{parts}}}"


def _prom_escape(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MetricsRegistry:
    """Thread-safe in-process counter and duration-summary registry with Prometheus export."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}

    @staticmethod
    def _norm_labels(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
        if not labels:
            return ()
        return tuple(sorted((str(k), str(v)) for k, v in labels.items()))

    def inc(
        self,
        name: str,
        amount: int = 1,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        key = (name, self._norm_labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + int(amount)

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        key = (name, self._norm_labels(labels))
        val = float(value)
        with self._lock:
            entry = self._histograms.get(key)
            if entry is None:
                # [count, sum, min, max]
                self._histograms[key] = [1.0, val, val, val]
            else:
                entry[0] += 1.0
                entry[1] += val
                if val < entry[2]:
                    entry[2] = val
                if val > entry[3]:
                    entry[3] = val

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {
                _format_metric_key(name, dict(lbls)): count
                for (name, lbls), count in sorted(self._counters.items())
            }
            histograms = {
                _format_metric_key(name, dict(lbls)): {
                    "count": int(stats[0]),
                    "sum": round(stats[1], 6),
                    "min": round(stats[2], 6),
                    "max": round(stats[3], 6),
                }
                for (name, lbls), stats in sorted(self._histograms.items())
            }
        return {"counters": counters, "histograms": histograms}

    def to_prometheus(self, *, extra_gauges: Mapping[str, int | float] | None = None) -> str:
        lines: list[str] = []
        if extra_gauges:
            for gname, gval in sorted(extra_gauges.items()):
                metric = f"quartz_{gname}"
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric} {gval}")

        with self._lock:
            seen_counter_types: set[str] = set()
            for (name, lbls), count in sorted(self._counters.items()):
                metric = f"quartz_{name}"
                if metric not in seen_counter_types:
                    lines.append(f"# TYPE {metric} counter")
                    seen_counter_types.add(metric)
                if lbls:
                    lbl_str = ",".join(f'{k}="{_prom_escape(v)}"' for k, v in lbls)
                    lines.append(f"{metric}{{{lbl_str}}} {count}")
                else:
                    lines.append(f"{metric} {count}")

            seen_hist_types: set[str] = set()
            for (name, lbls), stats in sorted(self._histograms.items()):
                metric = f"quartz_{name}"
                if metric not in seen_hist_types:
                    lines.append(f"# TYPE {metric} summary")
                    seen_hist_types.add(metric)
                lbl_suffix = (
                    "{" + ",".join(f'{k}="{_prom_escape(v)}"' for k, v in lbls) + "}"
                    if lbls
                    else ""
                )
                lines.append(f"{metric}_count{lbl_suffix} {int(stats[0])}")
                lines.append(f"{metric}_sum{lbl_suffix} {round(stats[1], 6)}")
        return "\n".join(lines) + ("\n" if lines else "")


_DEFAULT_AUDIT_LOGGER = AuditLogger()
_DEFAULT_METRICS = MetricsRegistry()


def get_audit_logger() -> AuditLogger:
    return _DEFAULT_AUDIT_LOGGER


def get_metrics_registry() -> MetricsRegistry:
    return _DEFAULT_METRICS
