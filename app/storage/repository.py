"""Data access for sessions, targets, samples, events and system samples.

Writes are batched (plan section 51): the monitor hands samples to
:meth:`Repository.queue_sample` and a periodic flush writes them in one
transaction, so a 250 ms sampling interval does not mean 4 disk writes a second
per target.
"""

from __future__ import annotations

import json
import statistics
import threading
from typing import Any, Iterable

from app.config.defaults import (
    DEFAULT_TARGETS,
    EventType,
    HealthStatus,
    Severity,
    TargetCategory,
)
from app.storage.database import Database, get_database
from app.storage.models import (
    Event,
    Incident,
    Sample,
    Session,
    SessionSummary,
    SystemSample,
    Target,
    TargetStats,
)
from app.utils.logger import get_logger
from app.utils.time import format_datetime, now_ts

log = get_logger("storage.repository")


class Repository:
    """All SQL lives here; the rest of the app speaks in dataclasses."""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or get_database()
        self.db.initialise()
        self._sample_queue: list[tuple] = []
        self._event_queue: list[Event] = []
        self._system_queue: list[SystemSample] = []
        self._queue_lock = threading.Lock()

    # ------------------------------------------------------------ sessions ---
    def create_session(self, name: str = "") -> Session:
        start = now_ts()
        name = name or f"Session {format_datetime(start)}"
        session_id = self.db.insert(
            "INSERT INTO sessions (start_time, end_time, name) VALUES (?, NULL, ?)",
            (start, name),
        )
        log.info("Session %s started", session_id)
        return Session(id=session_id, start_time=start, name=name)

    def end_session(self, session_id: int, end_time: float | None = None) -> None:
        self.db.execute(
            "UPDATE sessions SET end_time = ? WHERE id = ? AND end_time IS NULL",
            (end_time or now_ts(), session_id),
        )
        log.info("Session %s ended", session_id)

    def close_orphan_sessions(self) -> int:
        """Close sessions left open by a crash, using their last sample."""
        rows = self.db.query("SELECT id FROM sessions WHERE end_time IS NULL")
        closed = 0
        for row in rows:
            last = self.db.query_one(
                "SELECT MAX(timestamp) AS ts FROM samples WHERE session_id = ?", (row["id"],)
            )
            end = (last["ts"] if last and last["ts"] else None) or now_ts()
            self.db.execute("UPDATE sessions SET end_time = ? WHERE id = ?", (end, row["id"]))
            closed += 1
        if closed:
            log.info("Closed %s orphaned session(s)", closed)
        return closed

    def get_session(self, session_id: int) -> Session | None:
        row = self.db.query_one(
            "SELECT id, start_time, end_time, name FROM sessions WHERE id = ?", (session_id,)
        )
        return Session.from_row(row) if row else None

    def list_sessions(self, limit: int = 100, since: float | None = None) -> list[Session]:
        sql = "SELECT id, start_time, end_time, name FROM sessions"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE start_time >= ?"
            params.append(since)
        sql += " ORDER BY start_time DESC LIMIT ?"
        params.append(limit)
        return [Session.from_row(row) for row in self.db.query(sql, params)]

    # ------------------------------------------------------------- targets ---
    def list_targets(self, enabled_only: bool = False) -> list[Target]:
        sql = (
            "SELECT id, name, host, port, protocol, interval_ms, enabled, category "
            "FROM targets"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY CASE category WHEN 'gateway' THEN 0 WHEN 'internet' THEN 1 " \
               "ELSE 2 END, id"
        return [Target.from_row(row) for row in self.db.query(sql)]

    def get_target(self, target_id: int) -> Target | None:
        row = self.db.query_one(
            "SELECT id, name, host, port, protocol, interval_ms, enabled, category "
            "FROM targets WHERE id = ?",
            (target_id,),
        )
        return Target.from_row(row) if row else None

    def add_target(self, target: Target) -> Target:
        target.id = self.db.insert(
            "INSERT INTO targets (name, host, port, protocol, interval_ms, enabled, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target.name, target.host, target.port, target.protocol,
             target.interval_ms, int(target.enabled), target.category),
        )
        log.info("Target added: %s (%s)", target.name, target.display_host)
        return target

    def update_target(self, target: Target) -> None:
        if target.id is None:
            raise ValueError("Cannot update a target without an id")
        self.db.execute(
            "UPDATE targets SET name = ?, host = ?, port = ?, protocol = ?, "
            "interval_ms = ?, enabled = ?, category = ? WHERE id = ?",
            (target.name, target.host, target.port, target.protocol,
             target.interval_ms, int(target.enabled), target.category, target.id),
        )

    def delete_target(self, target_id: int) -> None:
        self.db.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        log.info("Target %s deleted", target_id)

    def set_target_enabled(self, target_id: int, enabled: bool) -> None:
        self.db.execute(
            "UPDATE targets SET enabled = ? WHERE id = ?", (int(enabled), target_id)
        )

    def ensure_default_targets(self, gateway_address: str | None = None) -> list[Target]:
        """Seed the default target set on first run (plan section 96)."""
        existing = self.list_targets()
        if existing:
            if gateway_address:
                for target in existing:
                    if target.category == TargetCategory.GATEWAY.value and (
                        target.host in ("auto", "") or target.host != gateway_address
                    ):
                        target.host = gateway_address
                        self.update_target(target)
            return self.list_targets()

        for spec in DEFAULT_TARGETS:
            host = spec["host"]
            if host == "auto":
                if not gateway_address:
                    continue  # no gateway detected: skip rather than guess
                host = gateway_address
            self.add_target(
                Target(
                    name=spec["name"],
                    host=host,
                    port=spec["port"],
                    protocol=spec["protocol"],
                    interval_ms=spec["interval_ms"],
                    enabled=spec["enabled"],
                    category=spec["category"],
                )
            )
        return self.list_targets()

    # ------------------------------------------------------------- samples ---
    def queue_sample(self, sample: Sample) -> None:
        with self._queue_lock:
            self._sample_queue.append(
                (sample.session_id, sample.target_id, sample.timestamp,
                 sample.latency_ms, int(sample.success), sample.error_type)
            )

    def queue_event(self, event: Event) -> None:
        with self._queue_lock:
            self._event_queue.append(event)

    def queue_system_sample(self, sample: SystemSample) -> None:
        with self._queue_lock:
            self._system_queue.append(sample)

    def pending_count(self) -> int:
        with self._queue_lock:
            return len(self._sample_queue) + len(self._event_queue) + len(self._system_queue)

    def flush(self) -> int:
        """Write everything queued in one transaction. Returns rows written."""
        with self._queue_lock:
            samples = self._sample_queue
            events = self._event_queue
            system = self._system_queue
            self._sample_queue = []
            self._event_queue = []
            self._system_queue = []

        if not samples and not events and not system:
            return 0

        written = 0
        try:
            with self.db.transaction() as conn:
                if samples:
                    conn.executemany(
                        "INSERT INTO samples (session_id, target_id, timestamp, latency_ms, "
                        "success, error_type) VALUES (?, ?, ?, ?, ?, ?)",
                        samples,
                    )
                    written += len(samples)
                if events:
                    conn.executemany(
                        "INSERT INTO events (session_id, timestamp, type, severity, target_id, "
                        "message, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (e.session_id, e.timestamp, e.type, e.severity, e.target_id,
                             e.message, e.metadata_json())
                            for e in events
                        ],
                    )
                    written += len(events)
                if system:
                    conn.executemany(
                        "INSERT INTO system_samples (session_id, timestamp, download_bps, "
                        "upload_bps, cpu_percent, memory_percent) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (s.session_id, s.timestamp, s.download_bps, s.upload_bps,
                             s.cpu_percent, s.memory_percent)
                            for s in system
                        ],
                    )
                    written += len(system)
        except Exception as exc:
            # One bad row (a target deleted mid-session, say) must not discard
            # the whole batch - retry row by row and drop only what fails.
            log.warning("Batch flush failed (%s); retrying rows individually", exc)
            written = self._flush_individually(samples, events, system)
        return written

    def _flush_individually(
        self, samples: list[tuple], events: list[Event], system: list[SystemSample]
    ) -> int:
        written = 0
        dropped = 0
        for row in samples:
            try:
                self.db.execute(
                    "INSERT INTO samples (session_id, target_id, timestamp, latency_ms, "
                    "success, error_type) VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
                written += 1
            except Exception:
                dropped += 1
        for event in events:
            try:
                self.insert_event(event)
                written += 1
            except Exception:
                dropped += 1
        for sample in system:
            try:
                self.db.execute(
                    "INSERT INTO system_samples (session_id, timestamp, download_bps, "
                    "upload_bps, cpu_percent, memory_percent) VALUES (?, ?, ?, ?, ?, ?)",
                    (sample.session_id, sample.timestamp, sample.download_bps,
                     sample.upload_bps, sample.cpu_percent, sample.memory_percent),
                )
                written += 1
            except Exception:
                dropped += 1
        if dropped:
            log.error("Dropped %s unwritable row(s) during flush", dropped)
        return written

    def insert_event(self, event: Event) -> int:
        """Write a single event immediately (used outside monitoring)."""
        return self.db.insert(
            "INSERT INTO events (session_id, timestamp, type, severity, target_id, message, "
            "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.session_id, event.timestamp, event.type, event.severity,
             event.target_id, event.message, event.metadata_json()),
        )

    def get_samples(
        self,
        session_id: int | None = None,
        target_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
    ) -> list[Sample]:
        sql = ("SELECT id, session_id, target_id, timestamp, latency_ms, success, error_type "
               "FROM samples WHERE 1=1")
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(end)
        sql += " ORDER BY timestamp"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            Sample(
                id=row["id"],
                session_id=row["session_id"],
                target_id=row["target_id"],
                timestamp=row["timestamp"],
                latency_ms=row["latency_ms"],
                success=bool(row["success"]),
                error_type=row["error_type"],
            )
            for row in self.db.query(sql, params)
        ]

    def get_downsampled_series(
        self,
        target_id: int,
        start: float,
        end: float,
        buckets: int = 600,
    ) -> list[tuple[float, float | None, float | None, float]]:
        """Bucketed history for the long-range graphs (plan section 36).

        Returns (bucket_time, average_ms, p95_ms, loss_fraction) per bucket.
        Percentiles need the raw values, so each bucket is aggregated in Python
        from a single ordered scan rather than with SQL window functions.
        """
        span = max(1e-6, end - start)
        width = span / max(1, buckets)
        rows = self.db.query(
            "SELECT timestamp, latency_ms, success FROM samples "
            "WHERE target_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (target_id, start, end),
        )
        result: list[tuple[float, float | None, float | None, float]] = []
        bucket_index = -1
        latencies: list[float] = []
        failures = 0
        total = 0

        def close_bucket(index: int) -> None:
            if total == 0:
                return
            average = sum(latencies) / len(latencies) if latencies else None
            p95 = TargetStats.percentile(latencies, 95) if latencies else None
            result.append((start + (index + 0.5) * width, average, p95, failures / total))

        for row in rows:
            index = min(buckets - 1, int((row["timestamp"] - start) / width))
            if index != bucket_index:
                close_bucket(bucket_index)
                bucket_index = index
                latencies = []
                failures = 0
                total = 0
            total += 1
            if row["success"] and row["latency_ms"] is not None:
                latencies.append(row["latency_ms"])
            else:
                failures += 1
        close_bucket(bucket_index)
        return result

    # -------------------------------------------------------------- events ---
    def get_events(
        self,
        session_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
        types: Iterable[str] | None = None,
        limit: int = 200,
    ) -> list[Event]:
        sql = ("SELECT id, session_id, timestamp, type, severity, target_id, message, "
               "metadata_json FROM events WHERE 1=1")
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(end)
        types = list(types or [])
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [Event.from_row(row) for row in self.db.query(sql, params)]

    def get_incidents(self, session_id: int | None = None, limit: int = 100) -> list[Incident]:
        events = self.get_events(
            session_id=session_id, types=[EventType.INCIDENT.value], limit=limit
        )
        incidents = []
        for event in events:
            incident = Incident.from_metadata(event.metadata)
            incident.session_id = event.session_id
            incidents.append(incident)
        return incidents

    # ------------------------------------------------------ system samples ---
    def get_system_samples(
        self, session_id: int | None = None, start: float | None = None,
        end: float | None = None, limit: int = 5000,
    ) -> list[SystemSample]:
        sql = ("SELECT id, session_id, timestamp, download_bps, upload_bps, cpu_percent, "
               "memory_percent FROM system_samples WHERE 1=1")
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if start is not None:
            sql += " AND timestamp >= ?"
            params.append(start)
        if end is not None:
            sql += " AND timestamp <= ?"
            params.append(end)
        sql += " ORDER BY timestamp LIMIT ?"
        params.append(limit)
        return [
            SystemSample(
                id=row["id"],
                session_id=row["session_id"],
                timestamp=row["timestamp"],
                download_bps=row["download_bps"],
                upload_bps=row["upload_bps"],
                cpu_percent=row["cpu_percent"],
                memory_percent=row["memory_percent"],
            )
            for row in self.db.query(sql, params)
        ]

    # --------------------------------------------------------- traceroutes ---
    def save_traceroute(self, target: str, hops: list[dict], session_id: int | None = None,
                        timestamp: float | None = None) -> int:
        return self.db.insert(
            "INSERT INTO traceroutes (session_id, timestamp, target, hops_json) "
            "VALUES (?, ?, ?, ?)",
            (session_id, timestamp or now_ts(), target, json.dumps(hops, default=str)),
        )

    def get_traceroutes(self, target: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT id, session_id, timestamp, target, hops_json FROM traceroutes"
        params: list[Any] = []
        if target:
            sql += " WHERE target = ?"
            params.append(target)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        results = []
        for row in self.db.query(sql, params):
            try:
                hops = json.loads(row["hops_json"])
            except (TypeError, ValueError):
                hops = []
            results.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "timestamp": row["timestamp"],
                "target": row["target"],
                "hops": hops,
            })
        return results

    # ----------------------------------------------------------- summaries ---
    def target_stats(
        self,
        target_id: int,
        session_id: int | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> TargetStats:
        """Percentile statistics over stored samples (plan section 37)."""
        samples = self.get_samples(session_id=session_id, target_id=target_id,
                                   start=start, end=end)
        latencies = [s.latency_ms for s in samples if s.success and s.latency_ms is not None]
        failed = sum(1 for s in samples if not s.success)
        target = self.get_target(target_id)
        stats = TargetStats.from_latencies(
            latencies, failed, target_id,
            target.name if target else str(target_id),
            target.category if target else TargetCategory.CUSTOM.value,
        )
        if len(latencies) > 1:
            diffs = [abs(latencies[i] - latencies[i - 1]) for i in range(1, len(latencies))]
            stats.jitter_ms = sum(diffs) / len(diffs)
            stats.baseline_ms = statistics.median(latencies)
        return stats

    def session_summary(self, session_id: int) -> SessionSummary | None:
        """Aggregate a stored session for the History page (plan section 35)."""
        session = self.get_session(session_id)
        if session is None:
            return None

        summary = SessionSummary(session=session)
        row = self.db.query_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed, "
            "AVG(CASE WHEN success = 1 THEN latency_ms END) AS avg_ms, "
            "MIN(CASE WHEN success = 1 THEN latency_ms END) AS min_ms, "
            "MAX(CASE WHEN success = 1 THEN latency_ms END) AS max_ms "
            "FROM samples WHERE session_id = ?",
            (session_id,),
        )
        if row and row["total"]:
            summary.sample_count = int(row["total"])
            failed = int(row["failed"] or 0)
            summary.loss_fraction = failed / summary.sample_count
            summary.average_ms = row["avg_ms"]
            summary.min_ms = row["min_ms"]
            summary.max_ms = row["max_ms"]

        latencies = [
            r["latency_ms"]
            for r in self.db.query(
                "SELECT latency_ms FROM samples WHERE session_id = ? AND success = 1 "
                "AND latency_ms IS NOT NULL ORDER BY latency_ms",
                (session_id,),
            )
        ]
        if latencies:
            summary.median_ms = statistics.median(latencies)
            summary.p95_ms = TargetStats.percentile(latencies, 95)
            summary.p99_ms = TargetStats.percentile(latencies, 99)

        spikes = self.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session_id, EventType.SPIKE.value, EventType.INCIDENT.value),
        )
        summary.spike_count = int(spikes["n"]) if spikes else 0
        events = self.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE session_id = ?", (session_id,)
        )
        summary.event_count = int(events["n"]) if events else 0

        summary.health_score, summary.status = self._score_summary(summary)
        return summary

    @staticmethod
    def _score_summary(summary: SessionSummary) -> tuple[int, HealthStatus]:
        """Health score for a stored session, reusing the live model."""
        from app.core.health_score import compute_health

        stats = TargetStats(
            target_name="session",
            category=TargetCategory.CUSTOM.value,
            average_ms=summary.average_ms,
            median_ms=summary.median_ms,
            p95_ms=summary.p95_ms,
            max_ms=summary.max_ms,
            loss_fraction=summary.loss_fraction,
            sample_count=summary.sample_count,
        )
        if summary.average_ms is not None and summary.p95_ms is not None:
            # Spread between average and p95 is a reasonable stand-in for jitter
            # when only aggregates were stored.
            stats.jitter_ms = max(0.0, summary.p95_ms - summary.average_ms) / 2
        minutes = max(0.1, summary.session.duration_s / 60.0)
        result = compute_health({0: stats}, summary.spike_count, minutes)
        return result.score, result.status

    def session_target_ids(self, session_id: int) -> list[int]:
        rows = self.db.query(
            "SELECT DISTINCT target_id FROM samples WHERE session_id = ? AND "
            "target_id IS NOT NULL",
            (session_id,),
        )
        return [int(row["target_id"]) for row in rows]

    def hourly_buckets(self, start: float, end: float) -> list[dict]:
        """Per-hour aggregates used by the History page overview."""
        rows = self.db.query(
            "SELECT CAST(timestamp / 3600 AS INTEGER) AS bucket, COUNT(*) AS total, "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed, "
            "AVG(CASE WHEN success = 1 THEN latency_ms END) AS avg_ms, "
            "MAX(CASE WHEN success = 1 THEN latency_ms END) AS max_ms "
            "FROM samples WHERE timestamp BETWEEN ? AND ? GROUP BY bucket ORDER BY bucket",
            (start, end),
        )
        return [
            {
                "start": row["bucket"] * 3600,
                "total": int(row["total"]),
                "failed": int(row["failed"] or 0),
                "average_ms": row["avg_ms"],
                "max_ms": row["max_ms"],
                "loss_fraction": (row["failed"] or 0) / row["total"] if row["total"] else 0.0,
            }
            for row in rows
        ]

    # ----------------------------------------------------------- retention ---
    def apply_retention(self, retention_days: int) -> int:
        """Delete data older than the retention window (plan section 40)."""
        if retention_days <= 0:
            return 0
        cutoff = now_ts() - retention_days * 86400
        deleted = 0
        with self.db.transaction() as conn:
            for table in ("samples", "events", "system_samples", "traceroutes"):
                cursor = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                deleted += cursor.rowcount if cursor.rowcount > 0 else 0
            conn.execute(
                "DELETE FROM sessions WHERE end_time IS NOT NULL AND end_time < ?", (cutoff,)
            )
        if deleted:
            log.info("Retention removed %s rows older than %s days", deleted, retention_days)
        return deleted

    def delete_session(self, session_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM samples WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM system_samples WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def clear_history(self) -> None:
        """Delete measurements but keep the configured targets."""
        with self.db.transaction() as conn:
            for table in ("samples", "events", "system_samples", "traceroutes", "sessions"):
                conn.execute(f"DELETE FROM {table}")
        self.db.vacuum()
        log.info("History cleared")

    def clear_database(self) -> None:
        """Delete everything, targets included."""
        with self.db.transaction() as conn:
            for table in ("samples", "events", "system_samples", "traceroutes",
                          "sessions", "targets"):
                conn.execute(f"DELETE FROM {table}")
        self.db.vacuum()
        log.info("Database cleared")

    def statistics(self) -> dict:
        counts = {}
        for table in ("sessions", "targets", "samples", "events", "system_samples"):
            row = self.db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(row["n"]) if row else 0
        oldest = self.db.query_one("SELECT MIN(timestamp) AS ts FROM samples")
        counts["oldest_sample"] = oldest["ts"] if oldest and oldest["ts"] else None
        counts["size_bytes"] = self.db.size_bytes()
        return counts


def severity_of(event: Event) -> Severity | None:
    return event.severity_enum
