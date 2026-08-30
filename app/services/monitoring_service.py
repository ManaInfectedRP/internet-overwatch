"""Qt adapter around the monitoring core (plan sections 47-48, 94).

The core runs on plain threads. This service subscribes to its callbacks and
re-emits them as Qt signals, which Qt delivers to the GUI thread through queued
connections. That is the whole reason the UI never blocks on a network call and
never touches a socket itself.

The UI does not repaint per sample either: measurements accumulate and a timer
pushes a coalesced update at the configured refresh rate (plan section 51).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QTimer, Signal

from app.config.defaults import DEFAULT_NOTIFICATION_COOLDOWN_S, Severity
from app.config.settings import Settings, get_settings
from app.core.monitor import Monitor
from app.core.simulator import SimulationRunner, Simulator
from app.network.gateway import detect_gateway
from app.storage.models import Event, HealthResult, Incident, Measurement, SystemSample, Target
from app.storage.repository import Repository
from app.utils.logger import get_logger
from app.utils.time import now_ts

log = get_logger("services.monitoring")


class MonitoringService(QObject):
    """Owns the monitor and publishes its state to the UI."""

    # Coalesced tick carrying the latest snapshot; the UI redraws from this.
    tick = Signal()
    measurement_received = Signal(object)   # Measurement
    event_logged = Signal(object)           # Event
    incident_detected = Signal(object)      # Incident
    system_sampled = Signal(object)         # SystemSample
    state_changed = Signal(str)             # running | paused | stopped
    targets_changed = Signal()
    notification_requested = Signal(str, str, str)  # title, body, severity

    def __init__(
        self,
        repository: Repository | None = None,
        settings: Settings | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or get_settings()
        self.repository = repository or Repository()
        self.monitor = Monitor(self.repository, self.settings)

        self._pending_lock = threading.Lock()
        self._pending: list[Measurement] = []
        self._last_notification: dict[str, float] = {}
        self._simulation: SimulationRunner | None = None
        self._simulated_targets: list[Target] = []

        self.monitor.on_measurement.append(self._queue_measurement)
        self.monitor.on_event.append(self._on_event)
        self.monitor.on_incident.append(self._on_incident)
        self.monitor.on_system_sample.append(self._on_system_sample)
        self.monitor.on_state_change.append(self._on_state_change)

        self._timer = QTimer(self)
        self._timer.setInterval(self.settings.ui_refresh_ms())
        self._timer.timeout.connect(self._flush_pending)

        self._retention_timer = QTimer(self)
        self._retention_timer.setInterval(3600 * 1000)  # hourly
        self._retention_timer.timeout.connect(self.apply_retention)

    # ----------------------------------------------------------- lifecycle ---
    @property
    def running(self) -> bool:
        return self.monitor.running or self._simulation is not None

    @property
    def paused(self) -> bool:
        return self.monitor.paused

    @property
    def simulating(self) -> bool:
        return self._simulation is not None

    def start(self, session_name: str = "") -> None:
        if self.running:
            return
        self.monitor.settings = self.settings
        self.monitor.start(session_name=session_name)
        self._timer.setInterval(self.settings.ui_refresh_ms())
        self._timer.start()
        self._retention_timer.start()
        # The monitor only resolves its target list on start, so the UI has to
        # be told to rebuild its per-target graph series now.
        self.targets_changed.emit()

    def stop(self) -> None:
        self.stop_simulation()
        if self.monitor.running:
            self.monitor.stop()
        self._timer.stop()
        self._retention_timer.stop()
        self._flush_pending()
        self.targets_changed.emit()

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def pause(self) -> None:
        if self.monitor.running:
            self.monitor.pause()

    def resume(self) -> None:
        if self.monitor.running:
            self.monitor.resume()

    def shutdown(self) -> None:
        """Stop everything and flush storage before the app exits."""
        try:
            self.stop()
        finally:
            self.repository.flush()

    # ---------------------------------------------------------- simulation ---
    def start_simulation(self, scenario: str) -> None:
        """Feed synthetic data through the real pipeline (plan section 65)."""
        self.stop_simulation()
        if self.monitor.running:
            self.monitor.stop()

        simulator = Simulator(scenario=scenario)
        self._simulated_targets = []
        self.monitor.targets.clear()
        self.monitor.buffers.clear()
        self.monitor.detector = type(self.monitor.detector)(self.settings.detection)

        self.monitor.session = self.repository.create_session(f"Simulation: {scenario}")
        for target in simulator.target_models():
            stored = self.repository.add_target(
                Target(
                    name=target.name,
                    host=target.host,
                    protocol=target.protocol,
                    interval_ms=target.interval_ms,
                    enabled=False,  # simulated targets are never probed for real
                    category=target.category,
                )
            )
            target.id = stored.id
            self._simulated_targets.append(stored)
            self.monitor.add_target(target)

        name_to_id = {t.name: t.id for t in simulator.target_models()}

        def sink(measurement: Measurement) -> None:
            measurement.target_id = name_to_id.get(measurement.target_name)
            self.monitor.ingest(measurement)

        self.monitor._started_at = now_ts()
        self._simulation = SimulationRunner(simulator, sink)
        self._simulation.start()
        self._timer.setInterval(self.settings.ui_refresh_ms())
        self._timer.start()
        self.state_changed.emit("simulating")
        self.targets_changed.emit()
        log.info("Simulation mode started: %s", scenario)

    def stop_simulation(self) -> None:
        if self._simulation is None:
            return
        self._simulation.stop()
        self._simulation.join(timeout=2.0)
        self._simulation = None
        for incident in self.monitor.detector.flush_all():
            self.monitor._handle_incident(incident)
        self.repository.flush()
        if self.monitor.session and self.monitor.session.id is not None:
            self.repository.end_session(self.monitor.session.id)
        for target in self._simulated_targets:
            if target.id is not None:
                self.repository.delete_target(target.id)
        self._simulated_targets.clear()
        self.monitor._started_at = None
        self._timer.stop()
        self.state_changed.emit("stopped")
        self.targets_changed.emit()
        log.info("Simulation mode stopped")

    # ------------------------------------------------------------ plumbing ---
    def _queue_measurement(self, measurement: Measurement) -> None:
        """Called from a probe thread - keep it cheap and thread-safe."""
        with self._pending_lock:
            self._pending.append(measurement)

    def _flush_pending(self) -> None:
        with self._pending_lock:
            pending = self._pending
            self._pending = []
        for measurement in pending:
            self.measurement_received.emit(measurement)
        self.tick.emit()

    def _on_event(self, event: Event) -> None:
        self.event_logged.emit(event)

    def _on_incident(self, incident: Incident) -> None:
        self.incident_detected.emit(incident)
        self._maybe_notify(incident)

    def _on_system_sample(self, sample: SystemSample) -> None:
        self.system_sampled.emit(sample)

    def _on_state_change(self, state: str) -> None:
        self.state_changed.emit(state)

    # ------------------------------------------------------- notifications ---
    def _maybe_notify(self, incident: Incident) -> None:
        """Desktop notification, rate limited per severity (plan section 43)."""
        config = self.settings.notifications
        if not config.enabled:
            return
        if incident.severity.rank < self.settings.min_notify_severity.rank:
            return

        key = f"{incident.target_id}:{incident.severity.value}"
        cooldown = config.cooldown_seconds or DEFAULT_NOTIFICATION_COOLDOWN_S
        last = self._last_notification.get(key, 0.0)
        now = now_ts()
        if now - last < cooldown:
            return
        self._last_notification[key] = now

        body = (
            f"{incident.target_name}: "
            f"{incident.baseline_latency_ms:.0f}ms -> {incident.peak_latency_ms:.0f}ms"
        )
        if incident.diagnosis:
            body += f"\n{incident.confidence.wording}: {incident.diagnosis}"
        self.notification_requested.emit(
            f"{incident.severity.label} lag spike detected", body, incident.severity.value
        )

    # --------------------------------------------------------------- state ---
    def health(self) -> HealthResult:
        return self.monitor.health()

    def stats(self, window: int | None = None):
        return self.monitor.stats(window)

    def report(self):
        return self.monitor.report()

    def targets(self) -> list[Target]:
        return self.monitor.target_list()

    def buffer(self, target_id: int | None):
        return self.monitor.buffer(target_id)

    def recent_events(self, limit: int = 50) -> list[Event]:
        return self.monitor.recent_events(limit)

    def recent_incidents(self, limit: int = 20) -> list[Incident]:
        return self.monitor.recent_incidents(limit)

    def uptime_s(self) -> float:
        return self.monitor.uptime_s

    def connectivity_state(self) -> str | None:
        return self.monitor.connectivity_state()

    def spike_count(self) -> int:
        return self.monitor.detector.total_spikes()

    def primary_target_id(self) -> int | None:
        return self.monitor.primary_target_id()

    def primary_stats(self):
        return self.monitor.primary_stats()

    def last_system_sample(self) -> SystemSample | None:
        return self.monitor.last_system_sample

    # ------------------------------------------------------------ settings ---
    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.monitor.apply_settings(settings)
        self._timer.setInterval(settings.ui_refresh_ms())

    def reload_targets(self) -> None:
        """Pick up edits made on the Targets page."""
        targets = self.repository.list_targets(enabled_only=True)
        if self.monitor.running:
            self.monitor.reload_targets(targets)
        self.targets_changed.emit()

    def ensure_targets(self) -> list[Target]:
        """Make sure a usable target set exists, detecting the gateway."""
        gateway = detect_gateway()
        return self.repository.ensure_default_targets(gateway.address)

    def apply_retention(self) -> None:
        try:
            self.repository.apply_retention(self.settings.storage.retention_days)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Retention pass failed: %s", exc)

    def severity_at_least(self, severity: Severity) -> bool:
        return severity.rank >= self.settings.min_notify_severity.rank
