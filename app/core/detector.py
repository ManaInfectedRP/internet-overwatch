"""Lag detection (plan sections 22-25, 87-89).

The detector adapts to the user's normal latency instead of comparing against a
fixed number: a 90 ms sample is unremarkable on a 85 ms connection and alarming
on a 12 ms one. Consecutive spikes are merged into a single incident so the
history reads as "one 3-second lag event" rather than forty separate rows.
"""

from __future__ import annotations

import statistics
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from app.config.defaults import (
    JITTER_EXCELLENT,
    JITTER_GOOD,
    JITTER_WARNING,
    MIN_BASELINE_SAMPLES,
    NodeStatus,
    Severity,
    TargetCategory,
)
from app.config.settings import DetectionSettings
from app.storage.models import Incident, Measurement, TargetStats
from app.utils.logger import get_logger

log = get_logger("core.detector")


def classify_severity(deviation_ms: float, thresholds: dict[Severity, float]) -> Severity | None:
    """Map a latency deviation onto a severity bucket (plan section 23)."""
    if deviation_ms is None:
        return None
    if deviation_ms >= thresholds[Severity.CRITICAL]:
        return Severity.CRITICAL
    if deviation_ms >= thresholds[Severity.SEVERE]:
        return Severity.SEVERE
    if deviation_ms >= thresholds[Severity.MODERATE]:
        return Severity.MODERATE
    if deviation_ms >= thresholds[Severity.MINOR]:
        return Severity.MINOR
    return None


def classify_jitter(jitter_ms: float | None) -> tuple[str, NodeStatus]:
    """Jitter quality band (plan section 21)."""
    if jitter_ms is None:
        return "Unknown", NodeStatus.UNKNOWN
    if jitter_ms < JITTER_EXCELLENT:
        return "Excellent", NodeStatus.HEALTHY
    if jitter_ms < JITTER_GOOD:
        return "Good", NodeStatus.HEALTHY
    if jitter_ms < JITTER_WARNING:
        return "Warning", NodeStatus.WARNING
    return "Poor", NodeStatus.PROBLEM


def classify_loss(fraction: float, settings: DetectionSettings | None = None) -> tuple[str, NodeStatus]:
    """Packet loss band (plan section 20)."""
    settings = settings or DetectionSettings()
    if fraction <= 0:
        return "None", NodeStatus.HEALTHY
    if fraction < settings.loss_minor:
        return "Minor", NodeStatus.HEALTHY
    if fraction < settings.loss_warning:
        return "Warning", NodeStatus.WARNING
    return "Serious", NodeStatus.PROBLEM


def rolling_median(values: list[float] | Deque[float]) -> float | None:
    """Median of the window; a median ignores the spikes we want to detect."""
    data = list(values)
    if not data:
        return None
    return statistics.median(data)


def mean_absolute_jitter(values: list[float] | Deque[float]) -> float | None:
    """Average consecutive-sample difference (plan section 25)."""
    data = list(values)
    if len(data) < 2:
        return None
    diffs = [abs(data[i] - data[i - 1]) for i in range(1, len(data))]
    return sum(diffs) / len(diffs)


@dataclass
class TargetDetector:
    """Per-target rolling state: baseline, jitter, loss and spike tracking."""

    target_id: int | None
    target_name: str
    category: str = TargetCategory.CUSTOM.value
    settings: DetectionSettings = field(default_factory=DetectionSettings)

    def __post_init__(self) -> None:
        self.latencies: Deque[float] = deque(maxlen=self.settings.rolling_window)
        self.outcomes: Deque[bool] = deque(maxlen=self.settings.loss_window_samples)
        self.recent: Deque[Measurement] = deque(maxlen=max(120, self.settings.rolling_window))
        self.jitter_window: Deque[float] = deque(maxlen=32)
        self.spike_count = 0
        self.total_samples = 0
        self.failed_samples = 0
        self.consecutive_failures = 0
        self.reachable = True
        self.last_error: str | None = None
        self.open_incident: Incident | None = None
        self._all_latencies: list[float] = []  # session-wide, for percentiles

    # ------------------------------------------------------------------ ---
    @property
    def baseline_ms(self) -> float | None:
        """Rolling median, or None until enough samples exist to trust it."""
        if len(self.latencies) < MIN_BASELINE_SAMPLES:
            return None
        return rolling_median(self.latencies)

    @property
    def jitter_ms(self) -> float | None:
        return mean_absolute_jitter(self.jitter_window)

    @property
    def loss_fraction(self) -> float:
        if not self.outcomes:
            return 0.0
        failures = sum(1 for ok in self.outcomes if not ok)
        return failures / len(self.outcomes)

    @property
    def session_loss_fraction(self) -> float:
        if not self.total_samples:
            return 0.0
        return self.failed_samples / self.total_samples

    # ------------------------------------------------------------- ingest ---
    def observe(self, measurement: Measurement) -> tuple[Measurement, Incident | None]:
        """Feed one measurement in.

        Returns the enriched measurement plus an incident when one has just
        closed (so the caller can log a single, deduplicated event).
        """
        self.total_samples += 1
        closed_incident: Incident | None = None

        if measurement.success and measurement.latency_ms is not None:
            self.consecutive_failures = 0
            if not self.reachable:
                self.reachable = True
            self.last_error = None
            self.jitter_window.append(measurement.latency_ms)
            self._all_latencies.append(measurement.latency_ms)
        else:
            self.failed_samples += 1
            self.consecutive_failures += 1
            self.last_error = measurement.error_type
            if self.consecutive_failures >= 3:
                self.reachable = False

        self.outcomes.append(bool(measurement.success))

        # Baseline is computed from the window *before* this sample so a spike
        # cannot pull its own baseline upward.
        baseline = self.baseline_ms
        measurement.baseline_ms = baseline
        measurement.jitter_ms = self.jitter_ms
        measurement.loss_fraction = self.loss_fraction

        if measurement.success and measurement.latency_ms is not None:
            severity = self._evaluate_spike(measurement, baseline)
            if severity is not None:
                measurement.is_spike = True
                measurement.severity = severity
                self.spike_count += 1
                closed_incident = self._extend_incident(measurement, baseline, severity)
            else:
                closed_incident = self._maybe_close_incident(measurement.timestamp)
                self.latencies.append(measurement.latency_ms)
        else:
            # A timeout during an incident belongs to that incident.
            if self.open_incident is not None:
                self.open_incident.end = measurement.timestamp
                self.open_incident.failed_count += 1
                self.open_incident.sample_count += 1
            else:
                closed_incident = self._maybe_close_incident(measurement.timestamp)

        self.recent.append(measurement)
        return measurement, closed_incident

    # ------------------------------------------------------------- spikes ---
    def _evaluate_spike(self, measurement: Measurement, baseline: float | None) -> Severity | None:
        """Spike test from plan section 87: absolute AND relative deviation."""
        if baseline is None or measurement.latency_ms is None:
            return None
        deviation = measurement.latency_ms - baseline
        if deviation < self.settings.spike_absolute_ms:
            return None
        if measurement.latency_ms < baseline * self.settings.spike_multiplier:
            return None
        severity = classify_severity(deviation, self.settings.severity_thresholds())
        # The detection threshold and the severity bands are configured
        # independently. If the user tunes detection below the minor band, the
        # spike is still a spike - it just lands in the lowest bucket.
        return severity or Severity.MINOR

    def _extend_incident(
        self, measurement: Measurement, baseline: float | None, severity: Severity
    ) -> Incident | None:
        """Start or continue an incident; return one that just closed."""
        closed: Incident | None = None
        gap = self.settings.incident_gap_seconds

        if self.open_incident is not None and measurement.timestamp - self.open_incident.end > gap:
            closed = self.close_incident()

        if self.open_incident is None:
            self.open_incident = Incident(
                id=uuid.uuid4().hex[:12],
                start=measurement.timestamp,
                end=measurement.timestamp,
                target_id=self.target_id,
                target_name=self.target_name,
                category=self.category,
                peak_latency_ms=measurement.latency_ms or 0.0,
                baseline_latency_ms=baseline or 0.0,
                severity=severity,
                sample_count=1,
                targets_affected=[self.target_name],
            )
        else:
            incident = self.open_incident
            incident.end = measurement.timestamp
            incident.sample_count += 1
            if (measurement.latency_ms or 0.0) > incident.peak_latency_ms:
                incident.peak_latency_ms = measurement.latency_ms or 0.0
            if severity.rank > incident.severity.rank:
                incident.severity = severity
        return closed

    def _maybe_close_incident(self, timestamp: float) -> Incident | None:
        if self.open_incident is None:
            return None
        if timestamp - self.open_incident.end > self.settings.incident_gap_seconds:
            return self.close_incident()
        return None

    def close_incident(self) -> Incident | None:
        """Finalise the open incident and hand it back."""
        incident = self.open_incident
        self.open_incident = None
        if incident is None:
            return None
        if incident.sample_count:
            incident.packet_loss = incident.failed_count / incident.sample_count
        log.debug(
            "Incident closed on %s: peak=%.0fms baseline=%.0fms duration=%.1fs severity=%s",
            self.target_name, incident.peak_latency_ms, incident.baseline_latency_ms,
            incident.duration_s, incident.severity.value,
        )
        return incident

    def flush(self) -> Incident | None:
        """Close any open incident, e.g. when monitoring stops."""
        return self.close_incident()

    # -------------------------------------------------------------- stats ---
    def stats(self, window: int | None = None) -> TargetStats:
        """Statistics over the recent window, or the whole session."""
        if window is None:
            latencies = list(self._all_latencies)
            failed = self.failed_samples
        else:
            recent = list(self.recent)[-window:]
            latencies = [m.latency_ms for m in recent
                         if m.success and m.latency_ms is not None]
            failed = sum(1 for m in recent if not m.success)

        stats = TargetStats.from_latencies(
            latencies, failed, self.target_id, self.target_name, self.category
        )
        stats.jitter_ms = self.jitter_ms
        stats.baseline_ms = self.baseline_ms
        stats.spike_count = self.spike_count
        stats.reachable = self.reachable
        stats.last_error = self.last_error
        if window is None:
            stats.loss_fraction = self.session_loss_fraction
        stats.status = self.status(stats)
        if self.recent:
            last = self.recent[-1]
            stats.current_ms = last.latency_ms if last.success else None
        return stats

    def recent_spike_count(self, seconds: float = 60.0) -> int:
        """Spikes seen in the last `seconds` of samples."""
        if not self.recent:
            return 0
        cutoff = self.recent[-1].timestamp - seconds
        return sum(1 for m in self.recent if m.timestamp >= cutoff and m.is_spike)

    def sustained_degradation(self, factor: float = 1.6) -> bool:
        """Detect a slow, spike-free rise in latency (plan section 87).

        The rolling baseline follows a gradual increase, so no sample ever
        looks like a spike. Comparing the recent window against the calmest
        part of the session catches the degradation the spike rule misses.
        """
        if len(self._all_latencies) < MIN_BASELINE_SAMPLES * 3:
            return False
        recent = self._all_latencies[-self.settings.rolling_window:]
        if len(recent) < MIN_BASELINE_SAMPLES:
            return False
        recent_median = statistics.median(recent)
        floor = statistics.median(sorted(self._all_latencies)[: max(5, len(self._all_latencies) // 4)])
        if floor <= 0:
            return False
        return recent_median > floor * factor and (recent_median - floor) >= 20.0

    def status(self, stats: TargetStats | None = None) -> NodeStatus:
        """Traffic-light status for this target (plan sections 12, 21, 72)."""
        from app.config.defaults import (
            GATEWAY_LATENCY_PROBLEM_MS,
            GATEWAY_LATENCY_WARNING_MS,
            LATENCY_PROBLEM_MS,
            LATENCY_WARNING_MS,
        )

        if not self.recent:
            return NodeStatus.UNKNOWN
        if not self.reachable:
            return NodeStatus.PROBLEM

        stats = stats or self.stats(window=self.settings.loss_window_samples)
        # The window average, not the last sample: a single lucky probe should
        # not turn a struggling target green.
        latency = stats.average_ms if stats.average_ms is not None else stats.current_ms
        if latency is None:
            return NodeStatus.PROBLEM

        if self.category == TargetCategory.GATEWAY.value:
            warn, problem = GATEWAY_LATENCY_WARNING_MS, GATEWAY_LATENCY_PROBLEM_MS
        else:
            warn, problem = LATENCY_WARNING_MS, LATENCY_PROBLEM_MS

        loss = self.loss_fraction
        jitter = self.jitter_ms
        spikes = self.recent_spike_count()

        if loss >= self.settings.loss_warning or latency >= problem or spikes >= 4:
            return NodeStatus.PROBLEM
        if (
            loss >= self.settings.loss_minor
            or latency >= warn
            or spikes >= 1
            or (jitter is not None and jitter >= self.settings.jitter_good_ms)
            or self.sustained_degradation()
        ):
            return NodeStatus.WARNING
        return NodeStatus.HEALTHY

    def measurements_between(self, start: float, end: float) -> list[Measurement]:
        """Samples inside a time window, used for spike correlation."""
        return [m for m in self.recent if start <= m.timestamp <= end]

    def reset(self) -> None:
        self.latencies.clear()
        self.outcomes.clear()
        self.recent.clear()
        self.jitter_window.clear()
        self._all_latencies.clear()
        self.spike_count = 0
        self.total_samples = 0
        self.failed_samples = 0
        self.consecutive_failures = 0
        self.reachable = True
        self.last_error = None
        self.open_incident = None


class Detector:
    """Owns one :class:`TargetDetector` per monitored target."""

    def __init__(self, settings: DetectionSettings | None = None) -> None:
        self.settings = settings or DetectionSettings()
        self.targets: dict[int | None, TargetDetector] = {}

    def register(
        self,
        target_id: int | None,
        name: str,
        category: str = TargetCategory.CUSTOM.value,
    ) -> TargetDetector:
        detector = TargetDetector(target_id, name, category, self.settings)
        self.targets[target_id] = detector
        return detector

    def unregister(self, target_id: int | None) -> None:
        self.targets.pop(target_id, None)

    def get(self, target_id: int | None) -> TargetDetector | None:
        return self.targets.get(target_id)

    def observe(self, measurement: Measurement) -> tuple[Measurement, Incident | None]:
        detector = self.targets.get(measurement.target_id)
        if detector is None:
            detector = self.register(
                measurement.target_id, measurement.target_name, measurement.category
            )
        return detector.observe(measurement)

    def stats(self, window: int | None = None) -> dict[int | None, TargetStats]:
        return {tid: det.stats(window) for tid, det in self.targets.items()}

    def by_category(self, category: str) -> list[TargetDetector]:
        return [d for d in self.targets.values() if d.category == category]

    def total_spikes(self) -> int:
        return sum(d.spike_count for d in self.targets.values())

    def flush_all(self) -> list[Incident]:
        incidents = [d.flush() for d in self.targets.values()]
        return [i for i in incidents if i is not None]

    def reset(self) -> None:
        for detector in self.targets.values():
            detector.reset()

    def apply_settings(self, settings: DetectionSettings) -> None:
        """Re-apply tuned thresholds without losing collected history."""
        self.settings = settings
        for detector in self.targets.values():
            detector.settings = settings
            detector.latencies = deque(detector.latencies, maxlen=settings.rolling_window)
            detector.outcomes = deque(detector.outcomes, maxlen=settings.loss_window_samples)
