"""The monitoring core (plan sections 47-51, 86).

`Monitor` ties together the pieces of the event pipeline::

    Probe -> Measurement -> Validation -> Ring buffer + Storage
                                       -> Detector -> Event -> Diagnosis -> UI

It is deliberately Qt-free: the UI subscribes through plain callbacks which
`MonitoringService` adapts into Qt signals. That keeps the whole engine unit
testable and makes the "never measure from the UI thread" rule from plan
section 94 structural rather than a convention.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque

from app.config.defaults import (
    DB_FLUSH_SECONDS,
    RING_BUFFER_SECONDS,
    EventType,
    NodeStatus,
    Protocol,
    Severity,
    TargetCategory,
)
from app.config.settings import Settings, get_settings
from app.core.detector import Detector
from app.core.diagnostics import DiagnosticReport, build_report, enrich_incident, offline_state
from app.core.health_score import compute_health
from app.core.scheduler import Scheduler
from app.network.ping import ErrorType, probe as run_probe
from app.network.throughput import ThroughputMonitor
from app.storage.models import (
    Event,
    HealthResult,
    Incident,
    Measurement,
    Session,
    SystemSample,
    Target,
    TargetStats,
)
from app.storage.repository import Repository
from app.utils.logger import get_logger
from app.utils.time import format_latency, now_ts

log = get_logger("core.monitor")

MeasurementCallback = Callable[[Measurement], None]
EventCallback = Callable[[Event], None]
IncidentCallback = Callable[[Incident], None]
SystemCallback = Callable[[SystemSample], None]
StateCallback = Callable[[str], None]


@dataclass
class RingBuffer:
    """In-memory recent samples for one target (plan section 50).

    The live graph reads from here so it never queries SQLite per frame.
    """

    seconds: float = RING_BUFFER_SECONDS
    timestamps: Deque[float] = field(default_factory=deque)
    latencies: Deque[float | None] = field(default_factory=deque)
    successes: Deque[bool] = field(default_factory=deque)
    spikes: Deque[tuple[float, float, str]] = field(default_factory=lambda: deque(maxlen=500))

    def append(self, measurement: Measurement) -> None:
        self.timestamps.append(measurement.timestamp)
        self.latencies.append(measurement.latency_ms if measurement.success else None)
        self.successes.append(measurement.success)
        if measurement.is_spike and measurement.latency_ms is not None:
            severity = measurement.severity.value if measurement.severity else Severity.MINOR.value
            self.spikes.append((measurement.timestamp, measurement.latency_ms, severity))
        self._trim(measurement.timestamp)

    def _trim(self, now: float) -> None:
        cutoff = now - self.seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
            self.latencies.popleft()
            self.successes.popleft()
        while self.spikes and self.spikes[0][0] < cutoff:
            self.spikes.popleft()

    def series(self, since: float | None = None) -> tuple[list[float], list[float | None]]:
        """(timestamps, latencies) with None marking failed probes."""
        if since is None:
            return list(self.timestamps), list(self.latencies)
        times: list[float] = []
        values: list[float | None] = []
        for ts, latency in zip(self.timestamps, self.latencies):
            if ts >= since:
                times.append(ts)
                values.append(latency)
        return times, values

    def spike_markers(self, since: float | None = None) -> list[tuple[float, float, str]]:
        if since is None:
            return list(self.spikes)
        return [s for s in self.spikes if s[0] >= since]

    def clear(self) -> None:
        self.timestamps.clear()
        self.latencies.clear()
        self.successes.clear()
        self.spikes.clear()


class Monitor:
    """Runs a monitoring session end to end."""

    def __init__(
        self,
        repository: Repository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or Repository()
        self.detector = Detector(self.settings.detection)
        self.scheduler = Scheduler()
        self.throughput = ThroughputMonitor()

        self.session: Session | None = None
        self.targets: dict[int | None, Target] = {}
        self.buffers: dict[int | None, RingBuffer] = {}
        self.events: Deque[Event] = deque(maxlen=500)
        self.incidents: Deque[Incident] = deque(maxlen=200)
        self.last_system_sample: SystemSample | None = None

        self._lock = threading.RLock()
        self._running = False
        self._started_at: float | None = None
        self._offline_state: str | None = None
        self._notified_unreachable: set[int | None] = set()

        self.on_measurement: list[MeasurementCallback] = []
        self.on_event: list[EventCallback] = []
        self.on_incident: list[IncidentCallback] = []
        self.on_system_sample: list[SystemCallback] = []
        self.on_state_change: list[StateCallback] = []

    # ----------------------------------------------------------- lifecycle ---
    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self.scheduler.paused

    @property
    def uptime_s(self) -> float:
        """Time monitoring has been running, or the length of the last session."""
        if self._started_at is None:
            return self.session.duration_s if self.session else 0.0
        return now_ts() - self._started_at

    def start(self, targets: list[Target] | None = None, session_name: str = "") -> Session:
        """Begin a monitoring session (plan section 38)."""
        with self._lock:
            if self._running:
                return self.session  # type: ignore[return-value]

            if targets is None:
                targets = self.repository.list_targets(enabled_only=True)
            targets = [t for t in targets if t.enabled and t.host]
            if not targets:
                log.warning("Starting monitoring with no enabled targets")

            self.session = self.repository.create_session(session_name)
            self._started_at = now_ts()
            self.detector = Detector(self.settings.detection)
            self.targets.clear()
            self.buffers.clear()
            self._notified_unreachable.clear()
            self._offline_state = None

            for target in targets:
                self.targets[target.id] = target
                self.buffers[target.id] = RingBuffer()
                self.detector.register(target.id, target.name, target.category)
                self.scheduler.add_target(
                    target, self._probe_target, self._interval_for(target)
                )

            self.scheduler.add_periodic(
                "db-flush", self._flush_storage,
                max(0.5, self.settings.storage.flush_seconds or DB_FLUSH_SECONDS),
            )
            if self.settings.monitoring.system_sampling_enabled:
                self.throughput.reset()
                self.scheduler.add_periodic("system-sampler", self._sample_system, 1.0)

            self.scheduler.start()
            self._running = True

        self._emit_event(
            Event(
                timestamp=now_ts(),
                type=EventType.MONITORING_STARTED.value,
                message=f"Monitoring started with {len(targets)} target(s)",
                session_id=self.session.id if self.session else None,
                metadata={"targets": [t.name for t in targets]},
            )
        )
        self._notify_state("running")
        log.info("Monitoring started (session %s, %s targets)",
                 self.session.id if self.session else "?", len(targets))
        return self.session  # type: ignore[return-value]

    def stop(self) -> None:
        """End the session, closing open incidents and flushing storage."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.scheduler.stop()

        for incident in self.detector.flush_all():
            self._handle_incident(incident)

        self._emit_event(
            Event(
                timestamp=now_ts(),
                type=EventType.MONITORING_STOPPED.value,
                message="Monitoring stopped",
                session_id=self.session.id if self.session else None,
            )
        )
        self._flush_storage()
        ended_at = now_ts()
        if self.session and self.session.id is not None:
            self.repository.end_session(self.session.id, ended_at)
            # Keep the in-memory session in step with the stored row so a report
            # generated after stopping shows the real period, not "ongoing".
            self.session.end_time = ended_at
        self._started_at = None
        self._notify_state("stopped")
        log.info("Monitoring stopped")

    def pause(self) -> None:
        self.scheduler.pause()
        self._notify_state("paused")

    def resume(self) -> None:
        self.scheduler.resume()
        self._notify_state("running")

    # ------------------------------------------------------------- targets ---
    def _interval_for(self, target: Target) -> int:
        """Interval for a target, honouring gaming-mode prioritisation."""
        interval = target.interval_ms or self.settings.monitoring.interval_for(target.category)
        gaming = self.settings.gaming
        if gaming.enabled and gaming.prioritize_custom_target:
            if target.category == TargetCategory.CUSTOM.value:
                interval = min(interval, self.settings.monitoring.custom_interval_ms)
            else:
                interval = max(interval, self.settings.monitoring.internet_interval_ms)
        return interval

    def add_target(self, target: Target) -> None:
        """Add a target to a running session."""
        with self._lock:
            self.targets[target.id] = target
            self.buffers.setdefault(target.id, RingBuffer())
            self.detector.register(target.id, target.name, target.category)
            if self._running:
                self.scheduler.add_target(target, self._probe_target, self._interval_for(target))

    def remove_target(self, target_id: int | None) -> None:
        with self._lock:
            self.scheduler.remove_target(target_id)
            self.targets.pop(target_id, None)
            self.buffers.pop(target_id, None)
            self.detector.unregister(target_id)

    def reload_targets(self, targets: list[Target]) -> None:
        """Apply an edited target list without dropping the session."""
        with self._lock:
            wanted = {t.id: t for t in targets if t.enabled and t.host}
            for target_id in list(self.targets):
                if target_id not in wanted:
                    self.remove_target(target_id)
            for target_id, target in wanted.items():
                if target_id in self.targets:
                    self.targets[target_id] = target
                    self.scheduler.set_interval(target_id, self._interval_for(target))
                    detector = self.detector.get(target_id)
                    if detector is not None:
                        detector.target_name = target.name
                        detector.category = target.category
                else:
                    self.add_target(target)

    def apply_settings(self, settings: Settings) -> None:
        """Re-apply settings live (intervals, thresholds, flush cadence)."""
        self.settings = settings
        self.detector.apply_settings(settings.detection)
        with self._lock:
            for target_id, target in self.targets.items():
                self.scheduler.set_interval(target_id, self._interval_for(target))

    # -------------------------------------------------------------- probes ---
    def _probe_target(self, target: Target) -> None:
        """Run one probe and push it through the pipeline. Runs off-thread."""
        result = run_probe(
            host=target.host,
            protocol=target.protocol,
            port=target.port,
            timeout_ms=self.settings.monitoring.timeout_ms,
            ip_version=self.settings.ip_version,
            implementation=self.settings.advanced.ping_implementation,
            dns_query_host=self.settings.advanced.dns_probe_host,
        )

        measurement = Measurement(
            target_id=target.id,
            target_name=target.name,
            category=target.category,
            timestamp=result.timestamp,
            success=result.success,
            latency_ms=result.latency_ms,
            error_type=result.error_type,
            protocol=result.protocol or target.protocol,
        )
        self.ingest(measurement)

    def ingest(self, measurement: Measurement) -> Measurement:
        """Validate, store, detect and publish one measurement.

        Exposed separately from `_probe_target` so the simulator and the tests
        can drive the exact same pipeline.
        """
        if measurement.success and (
            measurement.latency_ms is None or measurement.latency_ms < 0
        ):
            # Validation step of the pipeline: a "successful" probe with no
            # usable time is treated as a failure rather than poisoning stats.
            measurement.success = False
            measurement.latency_ms = None
            measurement.error_type = measurement.error_type or ErrorType.UNKNOWN

        measurement, incident = self.detector.observe(measurement)

        buffer = self.buffers.get(measurement.target_id)
        if buffer is None:
            buffer = RingBuffer()
            self.buffers[measurement.target_id] = buffer
        buffer.append(measurement)

        sample = measurement.sample
        sample.session_id = self.session.id if self.session else None
        self.repository.queue_sample(sample)

        for callback in list(self.on_measurement):
            self._safe_call(callback, measurement)

        if incident is not None:
            self._handle_incident(incident)
        self._check_reachability(measurement)
        return measurement

    def _check_reachability(self, measurement: Measurement) -> None:
        """Raise unreachable/recovered events once per state change."""
        detector = self.detector.get(measurement.target_id)
        if detector is None:
            return
        target_id = measurement.target_id
        if not detector.reachable and target_id not in self._notified_unreachable:
            self._notified_unreachable.add(target_id)
            state = offline_state(self.detector)
            event_type = EventType.TARGET_UNREACHABLE
            if state and state.startswith("LOCAL NETWORK UNREACHABLE"):
                event_type = EventType.LOCAL_NETWORK_UNREACHABLE
            elif state and "INTERNET UNREACHABLE" in state:
                event_type = EventType.INTERNET_UNREACHABLE
            self._offline_state = state
            self._emit_event(
                Event(
                    timestamp=measurement.timestamp,
                    type=event_type.value,
                    severity=Severity.SEVERE.value,
                    target_id=target_id,
                    target_name=measurement.target_name,
                    message=f"{measurement.target_name} is unreachable "
                            f"({ErrorType.label(measurement.error_type) or 'no response'})",
                    session_id=self.session.id if self.session else None,
                    metadata={"target_name": measurement.target_name,
                              "error": measurement.error_type},
                )
            )
        elif detector.reachable and target_id in self._notified_unreachable:
            self._notified_unreachable.discard(target_id)
            self._offline_state = offline_state(self.detector)
            self._emit_event(
                Event(
                    timestamp=measurement.timestamp,
                    type=EventType.TARGET_RECOVERED.value,
                    target_id=target_id,
                    target_name=measurement.target_name,
                    message=f"{measurement.target_name} is reachable again",
                    session_id=self.session.id if self.session else None,
                    metadata={"target_name": measurement.target_name},
                )
            )

    def _handle_incident(self, incident: Incident) -> None:
        """Correlate, diagnose and publish a closed incident (section 88)."""
        incident.session_id = self.session.id if self.session else None
        enrich_incident(incident, self.detector)
        self.incidents.appendleft(incident)

        message = (
            f"{incident.severity.label} lag incident on {incident.target_name}: "
            f"{format_latency(incident.peak_latency_ms)} peak "
            f"(baseline {format_latency(incident.baseline_latency_ms)})"
        )
        self._emit_event(
            Event(
                timestamp=incident.start,
                type=EventType.INCIDENT.value,
                severity=incident.severity.value,
                target_id=incident.target_id,
                target_name=incident.target_name,
                message=message,
                session_id=incident.session_id,
                metadata={**incident.to_metadata(), "target_name": incident.target_name},
            )
        )
        for callback in list(self.on_incident):
            self._safe_call(callback, incident)

    # -------------------------------------------------------------- events ---
    def _emit_event(self, event: Event) -> None:
        self.events.appendleft(event)
        self.repository.queue_event(event)
        for callback in list(self.on_event):
            self._safe_call(callback, event)

    def emit_info(self, message: str, metadata: dict | None = None) -> None:
        self._emit_event(
            Event(
                timestamp=now_ts(),
                type=EventType.INFO.value,
                message=message,
                session_id=self.session.id if self.session else None,
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _safe_call(callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception as exc:  # pragma: no cover - subscriber safety
            log.warning("Callback %s failed: %s", getattr(callback, "__name__", callback), exc)

    def _notify_state(self, state: str) -> None:
        for callback in list(self.on_state_change):
            self._safe_call(callback, state)

    # ------------------------------------------------------- periodic work ---
    def _flush_storage(self) -> None:
        self.repository.flush()

    def _sample_system(self) -> None:
        sample = self.throughput.sample()
        if sample is None:
            return
        system_sample = SystemSample(
            timestamp=sample.timestamp,
            download_bps=sample.download_bps,
            upload_bps=sample.upload_bps,
            cpu_percent=sample.cpu_percent,
            memory_percent=sample.memory_percent,
            session_id=self.session.id if self.session else None,
        )
        self.last_system_sample = system_sample
        self.repository.queue_system_sample(system_sample)
        for callback in list(self.on_system_sample):
            self._safe_call(callback, system_sample)

    # --------------------------------------------------------- live state ---
    def stats(self, window: int | None = None) -> dict[int | None, TargetStats]:
        return self.detector.stats(window)

    def health(self) -> HealthResult:
        minutes = max(0.1, self.uptime_s / 60.0)
        return compute_health(self.detector.stats(), self.detector.total_spikes(), minutes)

    def report(self) -> DiagnosticReport:
        return build_report(self.detector, self.settings.detection)

    def primary_target_id(self) -> int | None:
        """The target the headline metrics describe."""
        for category in (TargetCategory.CUSTOM, TargetCategory.INTERNET, TargetCategory.GATEWAY):
            for target_id, target in self.targets.items():
                if target.category == category.value:
                    return target_id
        return next(iter(self.targets), None)

    def primary_stats(self) -> TargetStats | None:
        target_id = self.primary_target_id()
        if target_id is None:
            return None
        detector = self.detector.get(target_id)
        return detector.stats(self.settings.detection.loss_window_samples) if detector else None

    def connectivity_state(self) -> str | None:
        return self._offline_state

    def overall_status(self) -> NodeStatus:
        from app.core.health_score import overall_node_status

        return overall_node_status(self.detector.stats())

    def buffer(self, target_id: int | None) -> RingBuffer | None:
        return self.buffers.get(target_id)

    def recent_events(self, limit: int = 50) -> list[Event]:
        return list(self.events)[:limit]

    def recent_incidents(self, limit: int = 20) -> list[Incident]:
        return list(self.incidents)[:limit]

    def target_list(self) -> list[Target]:
        return list(self.targets.values())

    def worker_diagnostics(self) -> dict:
        return {
            name: {
                "probes": stats.probes,
                "errors": stats.errors,
                "last_duration_ms": round(stats.last_duration_ms, 1),
                "overruns": stats.overruns,
            }
            for name, stats in self.scheduler.worker_stats().items()
        }

    def measurement_label(self, target_id: int | None) -> str:
        target = self.targets.get(target_id)
        if target is None:
            return ""
        return Protocol(target.protocol).measurement_note if target.protocol else ""
