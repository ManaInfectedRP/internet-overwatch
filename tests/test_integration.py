"""Integration tests for the event pipeline (plan sections 49, 64, 86).

These drive the real Monitor - detector, ring buffer, storage and callbacks -
using synthetic measurements, so the whole chain is exercised without touching
the network.
"""

from __future__ import annotations

import time

import pytest

from app.config.defaults import EventType, Severity, TargetCategory
from app.config.settings import Settings
from app.core.monitor import Monitor, RingBuffer
from app.core.simulator import SCENARIOS, Simulator
from app.storage.database import Database
from app.storage.models import Measurement, Target
from app.storage.repository import Repository


@pytest.fixture()
def monitor():
    database = Database(":memory:")
    database.initialise()
    repository = Repository(database)
    settings = Settings()
    settings.monitoring.system_sampling_enabled = False
    monitor = Monitor(repository, settings)
    yield monitor
    if monitor.running:
        monitor.stop()
    database.close()


def register(monitor, name, category, target_id):
    target = monitor.repository.add_target(
        Target(name=name, host=f"{name}.test", category=category, interval_ms=500)
    )
    monitor.targets[target.id] = target
    monitor.buffers[target.id] = RingBuffer()
    monitor.detector.register(target.id, name, category)
    return target


def three_layer_targets(monitor):
    return (
        register(monitor, "Router", TargetCategory.GATEWAY.value, 1),
        register(monitor, "Cloudflare", TargetCategory.INTERNET.value, 2),
        register(monitor, "Game", TargetCategory.CUSTOM.value, 3),
    )


# ------------------------------------------------------------ ring buffer ---


def test_ring_buffer_trims_to_its_window():
    buffer = RingBuffer(seconds=10)
    now = time.time()
    for index in range(100):
        buffer.append(Measurement(1, "T", TargetCategory.CUSTOM.value,
                                  now + index, True, 20.0))
    times, _values = buffer.series()
    assert times[-1] - times[0] <= 10


def test_ring_buffer_marks_failures_as_gaps():
    buffer = RingBuffer()
    now = time.time()
    buffer.append(Measurement(1, "T", TargetCategory.CUSTOM.value, now, True, 20.0))
    buffer.append(Measurement(1, "T", TargetCategory.CUSTOM.value, now + 1, False, None))
    _times, values = buffer.series()
    assert values == [20.0, None]


def test_ring_buffer_records_spike_markers():
    buffer = RingBuffer()
    measurement = Measurement(1, "T", TargetCategory.CUSTOM.value, time.time(), True, 500.0)
    measurement.is_spike = True
    measurement.severity = Severity.SEVERE
    buffer.append(measurement)
    markers = buffer.spike_markers()
    assert markers and markers[0][1] == 500.0
    assert markers[0][2] == Severity.SEVERE.value


# --------------------------------------------------------------- pipeline ---


def test_measurement_flows_to_buffer_detector_and_storage(monitor):
    router, _, _ = three_layer_targets(monitor)
    monitor.session = monitor.repository.create_session("test")

    monitor.ingest(Measurement(router.id, "Router", TargetCategory.GATEWAY.value,
                               time.time(), True, 2.0))

    assert len(monitor.buffers[router.id].timestamps) == 1
    assert monitor.detector.get(router.id).total_samples == 1
    assert monitor.repository.pending_count() == 1

    monitor.repository.flush()
    assert monitor.repository.get_samples()[0].latency_ms == 2.0


def test_invalid_success_is_downgraded_to_a_failure(monitor):
    """The validation step: a 'success' with no usable time is a failure."""
    target = register(monitor, "T", TargetCategory.CUSTOM.value, 1)
    result = monitor.ingest(
        Measurement(target.id, "T", TargetCategory.CUSTOM.value, time.time(), True, None)
    )
    assert not result.success
    assert result.error_type is not None


def test_negative_latency_is_rejected(monitor):
    target = register(monitor, "T", TargetCategory.CUSTOM.value, 1)
    result = monitor.ingest(
        Measurement(target.id, "T", TargetCategory.CUSTOM.value, time.time(), True, -5.0)
    )
    assert not result.success


def test_callbacks_fire_for_measurements_events_and_incidents(monitor):
    _, _, game = three_layer_targets(monitor)
    monitor.session = monitor.repository.create_session("test")

    measurements, events, incidents = [], [], []
    monitor.on_measurement.append(measurements.append)
    monitor.on_event.append(events.append)
    monitor.on_incident.append(incidents.append)

    timestamp = time.time()
    for _ in range(20):
        monitor.ingest(Measurement(game.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, True, 80.0))
        timestamp += 0.5
    for _ in range(4):
        monitor.ingest(Measurement(game.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, True, 500.0))
        timestamp += 0.5
    for incident in monitor.detector.flush_all():
        monitor._handle_incident(incident)

    assert len(measurements) == 24
    assert len(incidents) == 1
    assert any(e.type == EventType.INCIDENT.value for e in events)


def test_a_failing_callback_does_not_break_monitoring(monitor):
    """A broken UI subscriber must not stop measurement."""
    target = register(monitor, "T", TargetCategory.CUSTOM.value, 1)

    def explode(_measurement):
        raise RuntimeError("subscriber is broken")

    received = []
    monitor.on_measurement.append(explode)
    monitor.on_measurement.append(received.append)

    monitor.ingest(Measurement(target.id, "T", TargetCategory.CUSTOM.value,
                               time.time(), True, 20.0))
    assert len(received) == 1


def test_unreachable_and_recovered_events_are_raised_once(monitor):
    target = register(monitor, "Game", TargetCategory.CUSTOM.value, 1)
    monitor.session = monitor.repository.create_session("test")
    events = []
    monitor.on_event.append(events.append)

    timestamp = time.time()
    for _ in range(6):
        monitor.ingest(Measurement(target.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, False, None, "timeout"))
        timestamp += 0.5

    unreachable = [e for e in events if "unreachable" in e.type]
    assert len(unreachable) == 1, "the event must not repeat every sample"

    for _ in range(2):
        monitor.ingest(Measurement(target.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, True, 80.0))
        timestamp += 0.5
    assert [e for e in events if e.type == EventType.TARGET_RECOVERED.value]


def test_incident_event_carries_full_metadata(monitor):
    _, _, game = three_layer_targets(monitor)
    monitor.session = monitor.repository.create_session("test")
    events = []
    monitor.on_event.append(events.append)

    timestamp = time.time()
    for index in range(30):
        value = 500.0 if index >= 25 else 80.0
        monitor.ingest(Measurement(game.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, True, value))
        timestamp += 0.5
    for incident in monitor.detector.flush_all():
        monitor._handle_incident(incident)

    incident_events = [e for e in events if e.type == EventType.INCIDENT.value]
    assert incident_events
    metadata = incident_events[0].metadata
    for key in ("peak_latency_ms", "baseline_latency_ms", "severity", "diagnosis",
                "confidence", "correlation"):
        assert key in metadata

    # And it round-trips through storage.
    monitor.repository.flush()
    stored = monitor.repository.get_incidents(monitor.session.id)
    assert stored and stored[0].peak_latency_ms == pytest.approx(500.0)


# -------------------------------------------------------- session handling ---


def test_start_and_stop_writes_a_complete_session(monitor):
    monitor.repository.ensure_default_targets("192.168.1.1")
    targets = monitor.repository.list_targets(enabled_only=True)
    session = monitor.start(targets[:1], session_name="integration")

    assert monitor.running
    assert session.id is not None
    monitor.stop()

    assert not monitor.running
    stored = monitor.repository.get_session(session.id)
    assert stored.end_time is not None
    assert monitor.session.end_time is not None, "in-memory session must match storage"
    assert monitor.uptime_s > 0, "a finished session still reports its length"


def test_stop_closes_open_incidents(monitor):
    _, _, game = three_layer_targets(monitor)
    monitor.session = monitor.repository.create_session("test")
    monitor._running = True

    timestamp = time.time()
    for index in range(30):
        value = 500.0 if index >= 25 else 80.0
        monitor.ingest(Measurement(game.id, "Game", TargetCategory.CUSTOM.value,
                                   timestamp, True, value))
        timestamp += 0.5

    assert monitor.detector.get(game.id).open_incident is not None
    monitor.stop()
    assert monitor.detector.get(game.id).open_incident is None
    assert monitor.recent_incidents(), "the open incident must be recorded, not lost"


def test_primary_target_prefers_the_game_server(monitor):
    router, cloudflare, game = three_layer_targets(monitor)
    assert monitor.primary_target_id() == game.id

    monitor.remove_target(game.id)
    assert monitor.primary_target_id() == cloudflare.id

    monitor.remove_target(cloudflare.id)
    assert monitor.primary_target_id() == router.id


# ------------------------------------------------------------- simulation ---


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_runs_through_the_pipeline(monitor, scenario):
    """Plan section 65: the dashboard must be testable without a real fault."""
    simulator = Simulator(scenario=scenario, seed=7)
    mapping = {}
    for target in simulator.target_models():
        stored = register(monitor, target.name, target.category, target.id)
        mapping[target.name] = stored.id
    monitor.session = monitor.repository.create_session(scenario)

    for measurement in simulator.stream(120):
        measurement.target_id = mapping[measurement.target_name]
        monitor.ingest(measurement)
    for incident in monitor.detector.flush_all():
        monitor._handle_incident(incident)

    health = monitor.health()
    assert 0 <= health.score <= 100
    assert monitor.report().diagnosis.headline

    monitor.repository.flush()
    assert monitor.repository.statistics()["samples"] == 120 * 4


def test_outage_scenario_reports_a_connectivity_state(monitor):
    simulator = Simulator(scenario="complete_outage", seed=3)
    mapping = {}
    for target in simulator.target_models():
        stored = register(monitor, target.name, target.category, target.id)
        mapping[target.name] = stored.id
    monitor.session = monitor.repository.create_session("outage")

    for measurement in simulator.stream(40):
        measurement.target_id = mapping[measurement.target_name]
        monitor.ingest(measurement)

    assert monitor.connectivity_state() is not None


def test_health_degrades_as_conditions_worsen(monitor):
    """Same pipeline, two scenarios: the score must reflect the difference."""
    def score_for(scenario):
        database = Database(":memory:")
        database.initialise()
        local = Monitor(Repository(database), Settings())
        mapping = {}
        simulator = Simulator(scenario=scenario, seed=5)
        for target in simulator.target_models():
            local.targets[target.id] = target
            local.buffers[target.id] = RingBuffer()
            local.detector.register(target.id, target.name, target.category)
            mapping[target.name] = target.id
        local.session = local.repository.create_session(scenario)
        local._started_at = time.time() - 120
        for measurement in simulator.stream(200):
            local.ingest(measurement)
        score = local.health().score
        database.close()
        return score

    assert score_for("stable") > score_for("jitter") > score_for("complete_outage")
