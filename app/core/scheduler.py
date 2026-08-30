"""Probe scheduling (plan sections 26, 47, 48).

One worker thread per target keeps the code simple and isolates failures: a
target that blocks for a full timeout delays only itself. Each worker sleeps to
an absolute next-run time so intervals do not drift, and skips (rather than
queues) a tick it has already missed, which prevents a burst of catch-up probes
after the machine wakes from sleep.

Nothing here touches Qt - the scheduler is plain threading so it can be tested
headlessly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.config.defaults import MIN_INTERVAL_MS
from app.storage.models import Target
from app.utils.logger import get_logger

log = get_logger("core.scheduler")

ProbeFunc = Callable[[Target], None]


@dataclass
class WorkerStats:
    probes: int = 0
    errors: int = 0
    last_run: float = 0.0
    last_duration_ms: float = 0.0
    overruns: int = 0


class TargetWorker(threading.Thread):
    """Probes a single target on its own interval."""

    def __init__(
        self,
        target: Target,
        probe_func: ProbeFunc,
        interval_ms: int,
        stop_event: threading.Event,
        pause_event: threading.Event | None = None,
    ) -> None:
        super().__init__(name=f"probe-{target.name}", daemon=True)
        self.target = target
        self.probe_func = probe_func
        self.interval_s = max(MIN_INTERVAL_MS, int(interval_ms)) / 1000.0
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.stats = WorkerStats()
        self._interval_lock = threading.Lock()

    def set_interval(self, interval_ms: int) -> None:
        with self._interval_lock:
            self.interval_s = max(MIN_INTERVAL_MS, int(interval_ms)) / 1000.0

    def _current_interval(self) -> float:
        with self._interval_lock:
            return self.interval_s

    def run(self) -> None:  # pragma: no cover - exercised via Monitor
        log.debug("Worker started for %s every %.0f ms",
                  self.target.name, self._current_interval() * 1000)
        next_run = time.monotonic()
        while not self.stop_event.is_set():
            if self.pause_event is not None and self.pause_event.is_set():
                if self.stop_event.wait(0.2):
                    break
                next_run = time.monotonic()
                continue

            started = time.monotonic()
            try:
                self.probe_func(self.target)
                self.stats.probes += 1
            except Exception as exc:
                # One failing target must never stop monitoring (plan section 59).
                self.stats.errors += 1
                log.warning("Probe failed for %s: %s", self.target.name, exc, exc_info=True)
            finished = time.monotonic()
            self.stats.last_run = finished
            self.stats.last_duration_ms = (finished - started) * 1000.0

            interval = self._current_interval()
            next_run += interval
            if next_run <= finished:
                # The probe took longer than its interval; resync instead of
                # firing a burst of overdue probes.
                self.stats.overruns += 1
                next_run = finished + interval
            if self.stop_event.wait(max(0.0, next_run - time.monotonic())):
                break
        log.debug("Worker stopped for %s", self.target.name)


class PeriodicWorker(threading.Thread):
    """Generic fixed-interval worker (system sampling, DB flush, retention)."""

    def __init__(
        self,
        name: str,
        func: Callable[[], None],
        interval_s: float,
        stop_event: threading.Event,
        run_immediately: bool = False,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.func = func
        self.interval_s = max(0.05, interval_s)
        self.stop_event = stop_event
        self.run_immediately = run_immediately
        self.errors = 0

    def run(self) -> None:  # pragma: no cover - exercised via Monitor
        if not self.run_immediately and self.stop_event.wait(self.interval_s):
            return
        while not self.stop_event.is_set():
            try:
                self.func()
            except Exception as exc:
                self.errors += 1
                log.warning("Periodic task %s failed: %s", self.name, exc, exc_info=True)
            if self.stop_event.wait(self.interval_s):
                break


class Scheduler:
    """Owns the worker threads for the current monitoring session."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._workers: dict[int | None, TargetWorker] = {}
        self._periodic: list[PeriodicWorker] = []
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def add_target(self, target: Target, probe_func: ProbeFunc, interval_ms: int) -> None:
        with self._lock:
            existing = self._workers.get(target.id)
            if existing is not None and existing.is_alive():
                existing.set_interval(interval_ms)
                existing.target = target
                return
            worker = TargetWorker(target, probe_func, interval_ms,
                                  self.stop_event, self.pause_event)
            self._workers[target.id] = worker
            if self._running:
                worker.start()

    def remove_target(self, target_id: int | None) -> None:
        """Drop a target. Its thread exits at the next stop check."""
        with self._lock:
            self._workers.pop(target_id, None)

    def set_interval(self, target_id: int | None, interval_ms: int) -> None:
        with self._lock:
            worker = self._workers.get(target_id)
        if worker is not None:
            worker.set_interval(interval_ms)

    def add_periodic(self, name: str, func: Callable[[], None], interval_s: float,
                     run_immediately: bool = False) -> None:
        worker = PeriodicWorker(name, func, interval_s, self.stop_event, run_immediately)
        self._periodic.append(worker)
        if self._running:
            worker.start()

    def start(self) -> None:
        if self._running:
            return
        self.stop_event.clear()
        self.pause_event.clear()
        self._running = True
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers + self._periodic:
            if not worker.is_alive():
                worker.start()
        log.info("Scheduler started with %s target workers", len(workers))

    def stop(self, timeout: float = 3.0) -> None:
        if not self._running:
            return
        self._running = False
        self.stop_event.set()
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers + self._periodic:
            if worker.is_alive():
                worker.join(timeout=timeout)
        with self._lock:
            self._workers.clear()
        self._periodic.clear()
        log.info("Scheduler stopped")

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def worker_stats(self) -> dict[str, WorkerStats]:
        with self._lock:
            return {worker.target.name: worker.stats for worker in self._workers.values()}

    def active_target_ids(self) -> list[int | None]:
        with self._lock:
            return list(self._workers.keys())
