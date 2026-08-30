"""Domain models shared by the monitoring core, the storage layer and the UI.

These are plain dataclasses rather than ORM entities: the schema is small and
stable (plan section 39), and keeping them dependency-free means the detector
and diagnostics engine can be unit tested without touching a database.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from app.config.defaults import (
    CATEGORY_COLORS,
    Confidence,
    EventType,
    HealthStatus,
    NodeStatus,
    Protocol,
    Severity,
    TargetCategory,
)
from app.utils.time import now_ts


@dataclass
class Target:
    """A monitored destination (plan section 27)."""

    id: int | None = None
    name: str = ""
    host: str = ""
    port: int | None = None
    protocol: str = Protocol.ICMP.value
    interval_ms: int = 500
    enabled: bool = True
    category: str = TargetCategory.CUSTOM.value

    @property
    def category_enum(self) -> TargetCategory:
        try:
            return TargetCategory(self.category)
        except ValueError:
            return TargetCategory.CUSTOM

    @property
    def protocol_enum(self) -> Protocol:
        try:
            return Protocol(self.protocol)
        except ValueError:
            return Protocol.ICMP

    @property
    def color(self) -> str:
        return CATEGORY_COLORS.get(self.category, "#B0BEC5")

    @property
    def display_host(self) -> str:
        if self.port:
            return f"{self.host}:{self.port}"
        return self.host

    @property
    def measurement_label(self) -> str:
        """Always tell the user what is actually being measured (section 28)."""
        return self.protocol_enum.measurement_note

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Iterable) -> "Target":
        values = list(row)
        return cls(
            id=values[0],
            name=values[1],
            host=values[2],
            port=values[3],
            protocol=values[4],
            interval_ms=values[5],
            enabled=bool(values[6]),
            category=values[7],
        )


@dataclass(slots=True)
class Sample:
    """One stored probe result (plan section 39 - samples)."""

    timestamp: float
    target_id: int | None
    latency_ms: float | None
    success: bool
    error_type: str | None = None
    session_id: int | None = None
    id: int | None = None

    @property
    def failed(self) -> bool:
        return not self.success


@dataclass(slots=True)
class Measurement:
    """A sample enriched with the derived state at the time it was taken.

    This is what travels through the event pipeline (plan section 49) and what
    the UI renders; only the plain :class:`Sample` fields are persisted.
    """

    target_id: int | None
    target_name: str
    category: str
    timestamp: float
    success: bool
    latency_ms: float | None = None
    error_type: str | None = None
    protocol: str = Protocol.ICMP.value
    baseline_ms: float | None = None
    jitter_ms: float | None = None
    loss_fraction: float = 0.0
    is_spike: bool = False
    severity: Severity | None = None

    @property
    def sample(self) -> Sample:
        return Sample(
            timestamp=self.timestamp,
            target_id=self.target_id,
            latency_ms=self.latency_ms,
            success=self.success,
            error_type=self.error_type,
        )

    @property
    def deviation_ms(self) -> float | None:
        if self.latency_ms is None or self.baseline_ms is None:
            return None
        return self.latency_ms - self.baseline_ms


@dataclass
class Event:
    """An entry in the event feed (plan section 13)."""

    timestamp: float
    type: str
    severity: str | None = None
    target_id: int | None = None
    target_name: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: int | None = None
    id: int | None = None

    @property
    def type_enum(self) -> EventType:
        try:
            return EventType(self.type)
        except ValueError:
            return EventType.INFO

    @property
    def severity_enum(self) -> Severity | None:
        if not self.severity:
            return None
        try:
            return Severity(self.severity)
        except ValueError:
            return None

    @property
    def status(self) -> NodeStatus:
        """Traffic-light status used for the icon in the feed."""
        severity = self.severity_enum
        if severity in (Severity.SEVERE, Severity.CRITICAL):
            return NodeStatus.PROBLEM
        if severity in (Severity.MINOR, Severity.MODERATE):
            return NodeStatus.WARNING
        if self.type_enum in (
            EventType.TARGET_UNREACHABLE,
            EventType.INTERNET_UNREACHABLE,
            EventType.LOCAL_NETWORK_UNREACHABLE,
        ):
            return NodeStatus.PROBLEM
        if self.type_enum in (EventType.PACKET_LOSS, EventType.HIGH_JITTER):
            return NodeStatus.WARNING
        if self.type_enum in (EventType.STABILIZED, EventType.TARGET_RECOVERED,
                              EventType.MONITORING_STARTED):
            return NodeStatus.HEALTHY
        return NodeStatus.UNKNOWN

    def metadata_json(self) -> str:
        try:
            return json.dumps(self.metadata, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return "{}"

    @classmethod
    def from_row(cls, row: Iterable) -> "Event":
        values = list(row)
        try:
            metadata = json.loads(values[7]) if values[7] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        return cls(
            id=values[0],
            session_id=values[1],
            timestamp=values[2],
            type=values[3],
            severity=values[4],
            target_id=values[5],
            message=values[6],
            metadata=metadata,
            target_name=metadata.get("target_name", ""),
        )


@dataclass
class Incident:
    """A merged run of spikes treated as one problem (plan sections 88-89)."""

    id: str = ""
    start: float = 0.0
    end: float = 0.0
    target_id: int | None = None
    target_name: str = ""
    category: str = TargetCategory.CUSTOM.value
    peak_latency_ms: float = 0.0
    baseline_latency_ms: float = 0.0
    severity: Severity = Severity.MINOR
    sample_count: int = 0
    failed_count: int = 0
    packet_loss: float = 0.0
    diagnosis: str = ""
    confidence: Confidence = Confidence.UNCLEAR
    targets_affected: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    correlation: dict[str, Any] = field(default_factory=dict)
    session_id: int | None = None

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def deviation_ms(self) -> float:
        return self.peak_latency_ms - self.baseline_latency_ms

    @property
    def ratio(self) -> float:
        if self.baseline_latency_ms <= 0:
            return 0.0
        return self.peak_latency_ms / self.baseline_latency_ms

    def to_metadata(self) -> dict[str, Any]:
        return {
            "incident_id": self.id,
            "start": self.start,
            "end": self.end,
            "duration_s": self.duration_s,
            "peak_latency_ms": self.peak_latency_ms,
            "baseline_latency_ms": self.baseline_latency_ms,
            "severity": self.severity.value,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "category": self.category,
            "sample_count": self.sample_count,
            "failed_count": self.failed_count,
            "packet_loss": self.packet_loss,
            "diagnosis": self.diagnosis,
            "confidence": self.confidence.value,
            "targets_affected": list(self.targets_affected),
            "evidence": list(self.evidence),
            "correlation": self.correlation,
        }

    @classmethod
    def from_metadata(cls, data: dict[str, Any]) -> "Incident":
        incident = cls(
            id=str(data.get("incident_id", "")),
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            target_id=data.get("target_id"),
            target_name=data.get("target_name", ""),
            category=data.get("category", TargetCategory.CUSTOM.value),
            peak_latency_ms=float(data.get("peak_latency_ms", 0.0)),
            baseline_latency_ms=float(data.get("baseline_latency_ms", 0.0)),
            sample_count=int(data.get("sample_count", 0)),
            failed_count=int(data.get("failed_count", 0)),
            packet_loss=float(data.get("packet_loss", 0.0)),
            diagnosis=data.get("diagnosis", ""),
            targets_affected=list(data.get("targets_affected", [])),
            evidence=list(data.get("evidence", [])),
            correlation=dict(data.get("correlation", {})),
        )
        try:
            incident.severity = Severity(data.get("severity", Severity.MINOR.value))
        except ValueError:
            incident.severity = Severity.MINOR
        try:
            incident.confidence = Confidence(data.get("confidence", Confidence.UNCLEAR.value))
        except ValueError:
            incident.confidence = Confidence.UNCLEAR
        return incident


@dataclass
class Session:
    """A monitoring session (plan section 38)."""

    id: int | None = None
    start_time: float = field(default_factory=now_ts)
    end_time: float | None = None
    name: str = ""

    @property
    def duration_s(self) -> float:
        end = self.end_time if self.end_time else now_ts()
        return max(0.0, end - self.start_time)

    @property
    def active(self) -> bool:
        return self.end_time is None

    @classmethod
    def from_row(cls, row: Iterable) -> "Session":
        values = list(row)
        return cls(id=values[0], start_time=values[1], end_time=values[2], name=values[3])


@dataclass(slots=True)
class SystemSample:
    """Throughput / system utilisation sample (plan section 39)."""

    timestamp: float
    download_bps: float
    upload_bps: float
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    session_id: int | None = None
    id: int | None = None


@dataclass
class TargetStats:
    """Rolling statistics for one target (plan sections 9, 37)."""

    target_id: int | None = None
    target_name: str = ""
    category: str = TargetCategory.CUSTOM.value
    current_ms: float | None = None
    average_ms: float | None = None
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    jitter_ms: float | None = None
    baseline_ms: float | None = None
    loss_fraction: float = 0.0
    sample_count: int = 0
    failed_count: int = 0
    spike_count: int = 0
    status: NodeStatus = NodeStatus.UNKNOWN
    reachable: bool = True
    last_error: str | None = None

    @property
    def loss_percent(self) -> float:
        return self.loss_fraction * 100.0

    @staticmethod
    def percentile(values: list[float], percent: float) -> float | None:
        """Nearest-rank percentile; robust for the small windows used here."""
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = max(1, min(len(ordered), int(round(percent / 100.0 * len(ordered) + 0.5))))
        return ordered[rank - 1]

    @classmethod
    def from_latencies(
        cls,
        latencies: list[float],
        failed: int = 0,
        target_id: int | None = None,
        target_name: str = "",
        category: str = TargetCategory.CUSTOM.value,
    ) -> "TargetStats":
        stats = cls(target_id=target_id, target_name=target_name, category=category)
        total = len(latencies) + failed
        stats.sample_count = total
        stats.failed_count = failed
        stats.loss_fraction = failed / total if total else 0.0
        if latencies:
            stats.current_ms = latencies[-1]
            stats.average_ms = sum(latencies) / len(latencies)
            stats.median_ms = statistics.median(latencies)
            stats.min_ms = min(latencies)
            stats.max_ms = max(latencies)
            stats.p95_ms = cls.percentile(latencies, 95)
            stats.p99_ms = cls.percentile(latencies, 99)
        return stats


@dataclass
class HealthResult:
    """Output of the health score model (plan section 67)."""

    score: int = 0
    status: HealthStatus = HealthStatus.CRITICAL
    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.status.label

    @property
    def node_status(self) -> NodeStatus:
        if self.status in (HealthStatus.EXCELLENT, HealthStatus.GOOD, HealthStatus.STABLE):
            return NodeStatus.HEALTHY
        if self.status == HealthStatus.UNSTABLE:
            return NodeStatus.WARNING
        return NodeStatus.PROBLEM


@dataclass
class Diagnosis:
    """A diagnostic conclusion with explicit confidence (plan sections 16, 68)."""

    headline: str = "No clear cause detected"
    detail: str = ""
    confidence: Confidence = Confidence.UNCLEAR
    evidence: list[str] = field(default_factory=list)
    rule: str = ""
    layer: str = ""

    @property
    def wording(self) -> str:
        if self.confidence == Confidence.UNCLEAR:
            return self.headline
        return f"{self.confidence.wording}: {self.headline}"


@dataclass
class SessionSummary:
    """Aggregated statistics for a stored session (plan section 35)."""

    session: Session
    health_score: int = 0
    status: HealthStatus = HealthStatus.STABLE
    average_ms: float | None = None
    median_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    max_ms: float | None = None
    min_ms: float | None = None
    loss_fraction: float = 0.0
    spike_count: int = 0
    sample_count: int = 0
    event_count: int = 0
