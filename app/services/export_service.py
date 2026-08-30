"""CSV and database export (plan sections 41, 40).

Exports are written with `csv` and UTF-8 BOM so the files open cleanly in
Excel, which is where a support ticket attachment usually ends up.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Iterable

from app.storage.models import Event, Incident, Sample, SystemSample
from app.storage.repository import Repository
from app.utils.logger import get_logger
from app.utils.time import format_datetime, format_duration

log = get_logger("services.export")

# BOM keeps Excel from mangling UTF-8; other tools ignore it.
ENCODING = "utf-8-sig"


def _open_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", newline="", encoding=ENCODING)
    return handle, csv.writer(handle)


def export_samples_csv(
    repository: Repository,
    path: Path | str,
    session_id: int | None = None,
    start: float | None = None,
    end: float | None = None,
) -> int:
    """Export raw samples (plan section 41). Returns the row count."""
    path = Path(path)
    target_names = {t.id: t.name for t in repository.list_targets()}
    samples = repository.get_samples(session_id=session_id, start=start, end=end)

    handle, writer = _open_writer(path)
    try:
        writer.writerow(["timestamp", "datetime", "target", "target_id",
                         "latency_ms", "success", "error_type"])
        for sample in samples:
            writer.writerow([
                f"{sample.timestamp:.3f}",
                format_datetime(sample.timestamp),
                target_names.get(sample.target_id, ""),
                sample.target_id if sample.target_id is not None else "",
                f"{sample.latency_ms:.3f}" if sample.latency_ms is not None else "",
                "true" if sample.success else "false",
                sample.error_type or "",
            ])
    finally:
        handle.close()
    log.info("Exported %s samples to %s", len(samples), path)
    return len(samples)


def export_events_csv(
    repository: Repository,
    path: Path | str,
    session_id: int | None = None,
) -> int:
    path = Path(path)
    events = repository.get_events(session_id=session_id, limit=100000)
    handle, writer = _open_writer(path)
    try:
        writer.writerow(["timestamp", "datetime", "type", "severity", "target", "message"])
        for event in sorted(events, key=lambda e: e.timestamp):
            writer.writerow([
                f"{event.timestamp:.3f}",
                format_datetime(event.timestamp),
                event.type,
                event.severity or "",
                event.target_name or event.metadata.get("target_name", ""),
                event.message,
            ])
    finally:
        handle.close()
    log.info("Exported %s events to %s", len(events), path)
    return len(events)


def export_incidents_csv(incidents: Iterable[Incident], path: Path | str) -> int:
    path = Path(path)
    incidents = list(incidents)
    handle, writer = _open_writer(path)
    try:
        writer.writerow([
            "start", "end", "duration_s", "target", "severity", "baseline_ms",
            "peak_ms", "increase_ms", "ratio", "packet_loss", "diagnosis", "confidence",
        ])
        for incident in incidents:
            writer.writerow([
                format_datetime(incident.start),
                format_datetime(incident.end),
                f"{incident.duration_s:.2f}",
                incident.target_name,
                incident.severity.value,
                f"{incident.baseline_latency_ms:.1f}",
                f"{incident.peak_latency_ms:.1f}",
                f"{incident.deviation_ms:.1f}",
                f"{incident.ratio:.2f}",
                f"{incident.packet_loss:.4f}",
                incident.diagnosis,
                incident.confidence.value,
            ])
    finally:
        handle.close()
    log.info("Exported %s incidents to %s", len(incidents), path)
    return len(incidents)


def export_system_samples_csv(
    repository: Repository, path: Path | str, session_id: int | None = None
) -> int:
    path = Path(path)
    samples = repository.get_system_samples(session_id=session_id, limit=1000000)
    handle, writer = _open_writer(path)
    try:
        writer.writerow(["timestamp", "datetime", "download_bps", "upload_bps",
                         "cpu_percent", "memory_percent"])
        for sample in samples:
            writer.writerow([
                f"{sample.timestamp:.3f}",
                format_datetime(sample.timestamp),
                f"{sample.download_bps:.0f}",
                f"{sample.upload_bps:.0f}",
                f"{sample.cpu_percent:.1f}",
                f"{sample.memory_percent:.1f}",
            ])
    finally:
        handle.close()
    return len(samples)


def export_session_summary_csv(repository: Repository, path: Path | str,
                               limit: int = 500) -> int:
    """One row per session, for spotting trends across days."""
    path = Path(path)
    sessions = repository.list_sessions(limit=limit)
    handle, writer = _open_writer(path)
    written = 0
    try:
        writer.writerow(["session_id", "name", "start", "end", "duration", "samples",
                         "average_ms", "median_ms", "p95_ms", "p99_ms", "max_ms",
                         "loss_percent", "spikes", "health_score", "status"])
        for session in sessions:
            if session.id is None:
                continue
            summary = repository.session_summary(session.id)
            if summary is None:
                continue
            writer.writerow([
                session.id,
                session.name,
                format_datetime(session.start_time),
                format_datetime(session.end_time) if session.end_time else "",
                format_duration(session.duration_s),
                summary.sample_count,
                f"{summary.average_ms:.1f}" if summary.average_ms else "",
                f"{summary.median_ms:.1f}" if summary.median_ms else "",
                f"{summary.p95_ms:.1f}" if summary.p95_ms else "",
                f"{summary.p99_ms:.1f}" if summary.p99_ms else "",
                f"{summary.max_ms:.1f}" if summary.max_ms else "",
                f"{summary.loss_fraction * 100:.2f}",
                summary.spike_count,
                summary.health_score,
                summary.status.label,
            ])
            written += 1
    finally:
        handle.close()
    return written


def export_database(repository: Repository, path: Path | str) -> bool:
    """Copy the SQLite file, checkpointing WAL first so it is complete."""
    destination = Path(path)
    source = repository.db.path
    if str(source) == ":memory:":
        log.warning("Cannot export an in-memory database")
        return False
    try:
        repository.flush()
        repository.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        log.info("Database exported to %s", destination)
        return True
    except (OSError, Exception) as exc:  # pragma: no cover - defensive
        log.error("Database export failed: %s", exc)
        return False


def export_text(text: str, path: Path | str) -> bool:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        log.error("Could not write %s: %s", path, exc)
        return False


def suggested_filename(prefix: str, extension: str = "csv",
                       timestamp: float | None = None) -> str:
    from app.utils.time import now_ts, to_datetime

    stamp = to_datetime(timestamp or now_ts()).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}.{extension}"
