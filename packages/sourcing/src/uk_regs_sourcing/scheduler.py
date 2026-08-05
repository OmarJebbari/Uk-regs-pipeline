"""
Scheduler — drives the monitor on a recurring schedule.

Two modes:
    * run_once()    — single poll cycle (used by cron / GitHub Actions / CLI)
    * Scheduler     — long-running loop using APScheduler (daemon thread)

The heavy lifting is in run_once(); the Scheduler class is just a thin wrapper
that calls it on an interval. We use a thread-based scheduler (not asyncio) so
this slots into the existing sync codebase without ceremony.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime

from uk_regs_sourcing.monitor import MonitorResult, SourceMonitor
from uk_regs_sourcing.registry import load_registry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  One-shot
# --------------------------------------------------------------------------- #
def run_once(
    *,
    registry_path: str | None = None,
    force: bool = False,
    apply: bool = False,
    skip_llm: bool = False,
    monitor: SourceMonitor | None = None,
) -> MonitorResult:
    """
    Run a single monitoring cycle: load registry, poll all enabled sources.

    This is the entrypoint for cron, GitHub Actions, and the CLI's ``--once``
    flag. Returns the MonitorResult for inspection/logging.
    """
    registry = load_registry(registry_path)
    mon = monitor or SourceMonitor()
    enabled = registry.enabled_sources()

    if not enabled:
        logger.info("no enabled sources; nothing to do")
        cycle = MonitorResult(started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
        return cycle

    logger.info("running monitor cycle over %d source(s)", len(enabled))
    cycle = mon.poll_all(enabled, force=force, apply=apply, skip_llm=skip_llm)
    logger.info(cycle.summary())
    return cycle


# --------------------------------------------------------------------------- #
#  Long-running scheduler
# --------------------------------------------------------------------------- #
class Scheduler:
    """
    APScheduler-backed loop. Runs run_once() on the shortest poll interval of
    any enabled source (or an explicit override).

    Usage:
        sched = Scheduler(interval_minutes=60)
        sched.start()           # non-blocking (background thread)
        ...
        sched.shutdown()        # graceful
    """

    def __init__(
        self,
        *,
        interval_minutes: int | None = None,
        force: bool = False,
        apply: bool = False,
        skip_llm: bool = False,
        registry_path: str | None = None,
    ) -> None:
        self.interval_minutes = interval_minutes
        self.force = force
        self.apply = apply
        self.skip_llm = skip_llm
        self.registry_path = registry_path
        self._scheduler: object | None = None  # BackgroundScheduler
        self._monitor: SourceMonitor | None = None

    def start(self) -> None:
        """Start the background scheduler (non-blocking)."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "apscheduler is required for the long-running scheduler. "
                "Install with: pip install apscheduler"
            ) from e

        if self._scheduler is not None:
            logger.warning("scheduler already running")
            return

        interval = self._resolve_interval()
        self._monitor = SourceMonitor()

        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(minutes=interval),
            id="uk-regs-monitor",
            next_run_time=datetime.now(UTC),  # run immediately, then every `interval`
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("scheduler started: polling every %d minute(s)", interval)

    def shutdown(self, wait: bool = True) -> None:
        """Stop the scheduler gracefully."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
            logger.info("scheduler stopped")

    def run_forever(self) -> None:
        """
        Start the scheduler and block until SIGINT/SIGTERM.

        Convenience for `if __name__ == "__main__"` and container entrypoints.
        """
        self.start()
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except (ValueError, AttributeError):  # pragma: no cover
            pass  # signals only work in main thread

        logger.info("running forever (Ctrl-C to stop)")
        try:
            while self._scheduler is not None:
                time.sleep(60)
        except KeyboardInterrupt:  # pragma: no cover
            pass
        finally:
            self.shutdown()

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #
    def _resolve_interval(self) -> int:
        """Determine the polling interval: explicit > min(source intervals)."""
        if self.interval_minutes is not None:
            return self.interval_minutes
        registry = load_registry(self.registry_path)
        enabled = registry.enabled_sources()
        if not enabled:
            return 1440  # default: daily
        return min(s.poll_interval_minutes for s in enabled)

    def _tick(self) -> None:
        """One scheduled execution."""
        try:
            run_once(
                registry_path=self.registry_path,
                force=self.force,
                apply=self.apply,
                skip_llm=self.skip_llm,
                monitor=self._monitor,
            )
        except Exception:  # noqa: BLE001
            logger.error("scheduled monitor cycle failed", exc_info=True)

    def _signal_handler(self, signum, frame) -> None:  # pragma: no cover
        logger.info("received signal %d; shutting down", signum)
        self.shutdown()
        sys.exit(0)
