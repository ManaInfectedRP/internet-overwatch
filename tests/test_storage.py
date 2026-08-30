"""Storage tests: schema, batching, retention, percentiles, export.

Covers plan sections 38-42.
"""

from __future__ import annotations

import csv
import time

import pytest

from app.config.defaults import EventType, Protocol, Severity, TargetCategory
from app.services import export_service
from app.storage.database import Database
from app.storage.models import Event, Sample, SystemSample, Target
from app.storage.repository import Repository


@pytest.fixture()
def repo():
    database = Database(":memory:")
    database.initialise()
    repository = Repository(database)
    yield repository
    database.close()


@pytest.fixture()
def seeded(repo):
    """A repository with one finished session of samples and one incident."""
    targets = repo.ensure_default_targets("192.168.1.1")
    session = repo.create_session("test session")
    start = time.time() - 120

    target = targets[1]
    for index in range(200):
        ok = index % 50 != 0
        repo.queue_sample(Sample(
            timestamp=start + index * 0.5,
            target_id=target.id,
            latency_ms=(20.0 + index % 11) if ok else None,
            success=ok,
            error_type=None if ok else "timeout",
            session_id=session.id,
        ))
    repo.queue_event(Event(
        timestamp=start + 30,
        type=EventType.INCIDENT.value,
        severity=Severity.SEVERE.value,
        target_id=target.id,
        message="Severe lag incident",
        metadata={
            "incident_id": "abc123",
            "peak_latency_ms": 480.0,
            "baseline_latency_ms": 24.0,
            "severity": Severity.SEVERE.value,
            "start": start + 30,
            "end": start + 32,
            "target_name": target.name,
        },
        session_id=session.id,
    ))
    repo.queue_system_sample(SystemSample(start + 10, 12_400_000, 1_200_000,
                                          5.0, 40.0, session.id))
    repo.flush()
    repo.end_session(session.id)
    return repo, session, target


# ------------------------------------------------------------------ schema --


def test_schema_creates_every_table(repo):
    rows = repo.db.query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row["name"] for row in rows}
    assert {"sessions", "targets", "samples", "events", "system_samples",
            "traceroutes"} <= names


def test_schema_version_is_recorded(repo):
    version = repo.db.query_one("PRAGMA user_version")[0]
    assert version >= 1


def test_initialise_is_idempotent(repo):
    repo.db._initialised = False
    repo.db.initialise()
    assert repo.list_targets() == []


# ----------------------------------------------------------------- targets --


def test_target_crud(repo):
    target = repo.add_target(Target(name="Game", host="game.example", port=443,
                                    protocol=Protocol.TCP.value, interval_ms=500,
                                    category=TargetCategory.CUSTOM.value))
    assert target.id is not None

    fetched = repo.get_target(target.id)
    assert fetched.name == "Game"
    assert fetched.port == 443
    assert fetched.display_host == "game.example:443"

    fetched.name = "Renamed"
    repo.update_target(fetched)
    assert repo.get_target(target.id).name == "Renamed"

    repo.set_target_enabled(target.id, False)
    assert repo.get_target(target.id).enabled is False
    assert repo.list_targets(enabled_only=True) == []

    repo.delete_target(target.id)
    assert repo.get_target(target.id) is None


def test_default_targets_use_the_detected_gateway(repo):
    targets = repo.ensure_default_targets("10.20.30.1")
    gateways = [t for t in targets if t.category == TargetCategory.GATEWAY.value]
    assert gateways and gateways[0].host == "10.20.30.1"


def test_default_targets_skip_the_gateway_when_undetected(repo):
    """Plan section 58: never invent a router address."""
    targets = repo.ensure_default_targets(None)
    assert not [t for t in targets if t.category == TargetCategory.GATEWAY.value]
    assert [t for t in targets if t.category == TargetCategory.INTERNET.value]


def test_default_targets_are_not_duplicated(repo):
    first = repo.ensure_default_targets("192.168.1.1")
    second = repo.ensure_default_targets("192.168.1.1")
    assert len(first) == len(second)


def test_gateway_host_is_corrected_on_a_subnet_change(repo):
    repo.ensure_default_targets("192.168.1.1")
    repo.ensure_default_targets("10.0.0.1")
    gateways = [t for t in repo.list_targets()
                if t.category == TargetCategory.GATEWAY.value]
    assert gateways[0].host == "10.0.0.1"


# ---------------------------------------------------------------- sessions --


def test_session_lifecycle(repo):
    session = repo.create_session("run")
    assert session.active
    repo.end_session(session.id)
    stored = repo.get_session(session.id)
    assert stored.end_time is not None
    assert not stored.active


def test_orphan_sessions_are_closed_on_startup(repo):
    """A crash must not leave a session open forever."""
    session = repo.create_session("crashed")
    repo.queue_sample(Sample(time.time(), None, 20.0, True, session_id=session.id))
    repo.flush()

    assert repo.close_orphan_sessions() == 1
    assert repo.get_session(session.id).end_time is not None
    assert repo.close_orphan_sessions() == 0


# ----------------------------------------------------------------- batching --


def test_samples_are_batched_until_flushed(repo):
    session = repo.create_session()
    for index in range(10):
        repo.queue_sample(Sample(time.time(), None, 20.0, True, session_id=session.id))
    assert repo.pending_count() == 10
    assert repo.db.query_one("SELECT COUNT(*) AS n FROM samples")["n"] == 0

    assert repo.flush() == 10
    assert repo.pending_count() == 0
    assert repo.db.query_one("SELECT COUNT(*) AS n FROM samples")["n"] == 10


def test_flush_of_an_empty_queue_is_free(repo):
    assert repo.flush() == 0


def test_one_bad_row_does_not_discard_the_batch(repo):
    """A target deleted mid-session must not cost us the whole buffer."""
    session = repo.create_session()
    repo.queue_sample(Sample(time.time(), None, 20.0, True, session_id=session.id))
    repo.queue_sample(Sample(time.time(), 9999, 21.0, True, session_id=session.id))
    repo.queue_sample(Sample(time.time(), None, 22.0, True, session_id=session.id))

    written = repo.flush()
    assert written == 2, "the two valid rows must survive"
    assert repo.db.query_one("SELECT COUNT(*) AS n FROM samples")["n"] == 2


# ------------------------------------------------------------- aggregation --


def test_session_summary_statistics(seeded):
    repo, session, _ = seeded
    summary = repo.session_summary(session.id)

    assert summary.sample_count == 200
    assert summary.loss_fraction == pytest.approx(4 / 200)
    assert summary.average_ms is not None
    assert summary.p95_ms >= summary.median_ms
    assert summary.p99_ms >= summary.p95_ms
    assert summary.spike_count == 1
    assert 0 <= summary.health_score <= 100


def test_session_summary_of_a_missing_session(repo):
    assert repo.session_summary(4242) is None


def test_target_stats_percentiles(seeded):
    repo, session, target = seeded
    stats = repo.target_stats(target.id, session.id)
    assert stats.sample_count == 200
    assert stats.failed_count == 4
    assert stats.min_ms is not None and stats.max_ms is not None
    assert stats.jitter_ms is not None


def test_downsampled_series_buckets(seeded):
    repo, session, target = seeded
    start = time.time() - 200
    series = repo.get_downsampled_series(target.id, start, time.time(), buckets=20)
    assert series
    assert len(series) <= 20
    for _timestamp, average, p95, loss in series:
        assert average is None or average > 0
        assert 0.0 <= loss <= 1.0


def test_incidents_are_reconstructed_from_event_metadata(seeded):
    repo, session, _ = seeded
    incidents = repo.get_incidents(session.id)
    assert len(incidents) == 1
    assert incidents[0].id == "abc123"
    assert incidents[0].peak_latency_ms == 480.0
    assert incidents[0].severity == Severity.SEVERE


def test_events_can_be_filtered_by_type(seeded):
    repo, session, _ = seeded
    events = repo.get_events(session_id=session.id, types=[EventType.INCIDENT.value])
    assert len(events) == 1
    assert repo.get_events(session_id=session.id,
                           types=[EventType.PACKET_LOSS.value]) == []


def test_traceroute_round_trip(repo):
    hops = [{"number": 1, "host": "192.168.1.1", "ip": "192.168.1.1",
             "times_ms": [2.0], "best_ms": 2.0}]
    repo.save_traceroute("1.1.1.1", hops)
    stored = repo.get_traceroutes("1.1.1.1")
    assert len(stored) == 1
    assert stored[0]["hops"][0]["ip"] == "192.168.1.1"


# ------------------------------------------------------------- retention ---


def test_retention_deletes_old_rows_only(repo):
    session = repo.create_session()
    old = time.time() - 40 * 86400
    recent = time.time() - 60
    repo.queue_sample(Sample(old, None, 20.0, True, session_id=session.id))
    repo.queue_sample(Sample(recent, None, 20.0, True, session_id=session.id))
    repo.flush()

    assert repo.apply_retention(30) >= 1
    remaining = repo.get_samples()
    assert len(remaining) == 1
    assert remaining[0].timestamp == pytest.approx(recent)


def test_retention_of_zero_keeps_everything(repo):
    session = repo.create_session()
    repo.queue_sample(Sample(time.time() - 10_000_000, None, 20.0, True,
                             session_id=session.id))
    repo.flush()
    assert repo.apply_retention(0) == 0
    assert len(repo.get_samples()) == 1


def test_clear_history_keeps_targets(seeded):
    repo, _, _ = seeded
    repo.clear_history()
    assert repo.get_samples() == []
    assert repo.list_sessions() == []
    assert repo.list_targets(), "targets must survive a history wipe"


def test_clear_database_removes_targets_too(seeded):
    repo, _, _ = seeded
    repo.clear_database()
    assert repo.list_targets() == []


def test_delete_session_cascades(seeded):
    repo, session, _ = seeded
    repo.delete_session(session.id)
    assert repo.get_session(session.id) is None
    assert repo.get_samples(session_id=session.id) == []
    assert repo.get_events(session_id=session.id) == []


def test_statistics_report(seeded):
    repo, _, _ = seeded
    stats = repo.statistics()
    assert stats["samples"] == 200
    assert stats["events"] == 1
    assert stats["sessions"] == 1
    assert stats["oldest_sample"] is not None


# ---------------------------------------------------------------- exports ---


def test_csv_export_has_a_header_and_rows(seeded, tmp_path):
    repo, session, _ = seeded
    path = tmp_path / "samples.csv"
    count = export_service.export_samples_csv(repo, path, session_id=session.id)
    assert count == 200

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["timestamp", "datetime", "target", "target_id",
                       "latency_ms", "success", "error_type"]
    assert len(rows) == 201
    assert any(row[5] == "false" for row in rows[1:]), "failures must be exported"


def test_events_and_incidents_export(seeded, tmp_path):
    repo, session, _ = seeded
    assert export_service.export_events_csv(repo, tmp_path / "e.csv",
                                            session_id=session.id) == 1
    incidents = repo.get_incidents(session.id)
    assert export_service.export_incidents_csv(incidents, tmp_path / "i.csv") == 1
    assert (tmp_path / "i.csv").read_text(encoding="utf-8-sig").count("\n") >= 2


def test_session_summary_export(seeded, tmp_path):
    repo, _, _ = seeded
    assert export_service.export_session_summary_csv(repo, tmp_path / "s.csv") == 1


def test_database_export_is_refused_for_memory(seeded, tmp_path):
    repo, _, _ = seeded
    assert export_service.export_database(repo, tmp_path / "copy.db") is False


def test_database_export_copies_a_file(tmp_path):
    path = tmp_path / "live.db"
    database = Database(path)
    database.initialise()
    repository = Repository(database)
    repository.create_session()
    repository.flush()

    destination = tmp_path / "exported.db"
    assert export_service.export_database(repository, destination) is True
    assert destination.exists() and destination.stat().st_size > 0
    database.close()
