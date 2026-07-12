# core/monitoring.py
"""
Database connection and job execution monitoring.

Provides:
- Automatic logging of every Django DB connection open/close event
  (thread name, thread ID, connection alias, active count).
- ``ConnectionSnapshot`` for point-in-time pool state.
- ``monitor_post_job()`` wrapper that logs per-job timing and
  connection deltas.

Activate by importing this module (``import core.monitoring``).
The DB backend patching happens at import time so no business-logic
files need to be modified.
"""

import logging
import threading
import time

from django.db.backends.base import base as base_backend

logger = logging.getLogger("db.monitoring")

# ---------------------------------------------------------------------------
# Connection tracker — counts open / close events per thread.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_active_by_alias: dict[str, int] = {}
_total_opens = 0
_total_closes = 0


def _on_open(alias):
    global _total_opens
    t = threading.current_thread()
    with _lock:
        _total_opens += 1
        _active_by_alias[alias] = _active_by_alias.get(alias, 0) + 1
        active = sum(_active_by_alias.values())
    logger.info(
        "CONN OPEN  alias=%s  thread=%s  tid=%d  "
        "active=%d  total_opens=%d",
        alias, t.name, t.ident, active, _total_opens,
    )


def _on_close(alias):
    global _total_closes
    t = threading.current_thread()
    with _lock:
        _total_closes += 1
        _active_by_alias[alias] = max(0, _active_by_alias.get(alias, 0) - 1)
        active = sum(_active_by_alias.values())
    logger.info(
        "CONN CLOSE alias=%s  thread=%s  tid=%d  "
        "active=%d  total_closes=%d",
        alias, t.name, t.ident, active, _total_closes,
    )


# ---------------------------------------------------------------------------
# Monkey-patch Django's DatabaseWrapper
# ---------------------------------------------------------------------------

_original_ensure = base_backend.BaseDatabaseWrapper.ensure_connection
_original_close = base_backend.BaseDatabaseWrapper.close


def _patched_ensure_connection(self):
    _on_open(self.alias)
    return _original_ensure(self)


def _patched_close(self):
    _on_close(self.alias)
    return _original_close(self)


base_backend.BaseDatabaseWrapper.ensure_connection = _patched_ensure_connection
base_backend.BaseDatabaseWrapper.close = _patched_close


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def connection_snapshot() -> dict:
    """Return a point-in-time snapshot of connection pool state."""
    with _lock:
        return {
            "active_by_alias": dict(_active_by_alias),
            "active_total": sum(_active_by_alias.values()),
            "total_opens": _total_opens,
            "total_closes": _total_closes,
        }


# ---------------------------------------------------------------------------
# Per-job timing wrapper
# ---------------------------------------------------------------------------

def monitor_post_job(job_id):
    """Log timing and connection deltas for a single ``process_post_job`` call.

    Usage::

        from core.monitoring import monitor_post_job

        result = monitor_post_job(job_id)  # calls the real function
    """
    import posts.tasks as _tasks_mod
    _real_fn = _tasks_mod.process_post_job

    t = threading.current_thread()
    snap_before = connection_snapshot()
    start = time.monotonic()

    logger.info(
        "JOB START job_id=%s  thread=%s  tid=%d  active_conns=%s",
        job_id, t.name, t.ident, snap_before["active_total"],
    )

    try:
        result = _real_fn(job_id)
    except Exception:
        elapsed = time.monotonic() - start
        snap_after = connection_snapshot()
        logger.exception(
            "JOB FAIL  job_id=%s  thread=%s  tid=%d  elapsed=%.2fs  "
            "active_conns=%d  opens=%d  closes=%d",
            job_id, t.name, t.ident, elapsed,
            snap_after["active_total"],
            snap_after["total_opens"] - snap_before["total_opens"],
            snap_after["total_closes"] - snap_before["total_closes"],
        )
        raise
    else:
        elapsed = time.monotonic() - start
        snap_after = connection_snapshot()
        logger.info(
            "JOB END   job_id=%s  status=%s  thread=%s  tid=%d  "
            "elapsed=%.2fs  active_conns=%d  opens=%d  closes=%d",
            job_id, result.get("status", "?"), t.name, t.ident, elapsed,
            snap_after["active_total"],
            snap_after["total_opens"] - snap_before["total_opens"],
            snap_after["total_closes"] - snap_before["total_closes"],
        )
        return result
