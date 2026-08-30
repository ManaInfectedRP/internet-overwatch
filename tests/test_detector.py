"""Detector tests: baseline, jitter, packet loss, spikes, severity, incidents.

Covers plan sections 20-25, 66 and 87-88.
"""

from __future__ import annotations

import pytest

from app.config.defaults import NodeStatus, Severity, TargetCategory
from app.config.settings import DetectionSettings
from app.core.detector import (
    Detector,
    TargetDetector,
    classify_jitter,
    classify_loss,
    classify_severity,
    mean_absolute_jitter,
    rolling_median,
)
from app.storage.models import Measurement


def make_measurement(value, timestamp, target_id=1, success=True,
                     category=TargetCategory.CUSTOM.value):
    return Measurement(
        target_id=target_id,
        target_name="Target",
        category=category,
        timestamp=timestamp,
        success=success,
        latency_ms=value,
        error_type=None if success else "timeout",
    )


def feed(detector, values, start=1000.0, step=0.5):
    """Push a list of latencies (None means a failed probe) into a detector."""
    incidents = []
    timestamp = start
    for value in values:
        measurement = make_measurement(value, timestamp, success=value is not None)
        _, incident = detector.observe(measurement)
        if incident is not None:
            incidents.append(incident)
        timestamp += step
    return incidents


# ---------------------------------------------------------------- primitives --


def test_rolling_median_ignores_outliers():
    assert rolling_median([20, 21, 22, 500]) == pytest.approx(21.5)
    assert rolling_median([]) is None


def test_mean_absolute_jitter():
    assert mean_absolute_jitter([10, 12, 11]) == pytest.approx(1.5)
    assert mean_absolute_jitter([10]) is None


def test_classify_severity_bands():
    thresholds = DetectionSettings().severity_thresholds()
    assert classify_severity(20, thresholds) is None
    assert classify_severity(60, thresholds) == Severity.MINOR
    assert classify_severity(150, thresholds) == Severity.MODERATE
    assert classify_severity(300, thresholds) == Severity.SEVERE
    assert classify_severity(900, thresholds) == Severity.CRITICAL


def test_classify_jitter_bands():
    assert classify_jitter(5)[0] == "Excellent"
    assert classify_jitter(15)[0] == "Good"
    assert classify_jitter(30)[0] == "Warning"
    assert classify_jitter(80)[0] == "Poor"
    assert classify_jitter(None)[1] == NodeStatus.UNKNOWN


def test_classify_loss_bands():
    assert classify_loss(0.0)[1] == NodeStatus.HEALTHY
    assert classify_loss(0.005)[0] == "Minor"
    assert classify_loss(0.02)[1] == NodeStatus.WARNING
    assert classify_loss(0.2)[1] == NodeStatus.PROBLEM


def test_severity_ordering():
    assert Severity.MINOR < Severity.MODERATE < Severity.SEVERE < Severity.CRITICAL
    assert Severity.CRITICAL > Severity.MINOR


# ------------------------------------------------------------------ baseline --


def test_baseline_needs_enough_samples():
    detector = TargetDetector(1, "T")
    feed(detector, [20, 21, 22])
    assert detector.baseline_ms is None, "baseline must not be trusted too early"

    feed(detector, [20, 21, 22, 20, 21, 22], start=1002.0)
    assert detector.baseline_ms == pytest.approx(21, abs=1.5)


def test_baseline_is_not_dragged_up_by_a_spike():
    """A spike must not be admitted into its own baseline window."""
    detector = TargetDetector(1, "T")
    feed(detector, [20] * 20)
    baseline_before = detector.baseline_ms
    feed(detector, [400], start=1100.0)
    assert detector.baseline_ms == pytest.approx(baseline_before)


# -------------------------------------------------------------------- spikes --


def test_plan_scenario_66_detects_two_spikes():
    """The worked example from plan section 66."""
    detector = TargetDetector(1, "Game")
    values = [24, 26, 25, 27, 29, 25, 26, 24, 25, 26, 27, 25, 181, 430, 27, 26, 25]
    feed(detector, values)
    assert detector.spike_count == 2

    incident = detector.flush()
    assert incident is not None
    assert incident.severity == Severity.SEVERE
    assert incident.peak_latency_ms == 430


def test_spike_requires_both_absolute_and_relative_deviation():
    """A high but proportionally small rise on a slow link is not a spike."""
    detector = TargetDetector(1, "Slow link")
    feed(detector, [200] * 20)
    # +80 ms is over the absolute threshold but only 1.4x the baseline.
    feed(detector, [280], start=1100.0)
    assert detector.spike_count == 0

    # 3x the baseline is a spike.
    feed(detector, [600], start=1200.0)
    assert detector.spike_count == 1


def test_small_absolute_rise_on_fast_link_is_not_a_spike():
    detector = TargetDetector(1, "Fast link")
    feed(detector, [2] * 20)
    feed(detector, [20], start=1100.0)  # 10x baseline but only +18 ms
    assert detector.spike_count == 0


def test_sensitivity_is_configurable():
    settings = DetectionSettings(spike_absolute_ms=10.0, spike_multiplier=1.2)
    detector = TargetDetector(1, "T", settings=settings)
    feed(detector, [20] * 20)
    feed(detector, [40], start=1100.0)
    assert detector.spike_count == 1


# ----------------------------------------------------------------- incidents --


def test_consecutive_spikes_merge_into_one_incident():
    """Plan section 88: one lag event, not four separate rows."""
    detector = TargetDetector(1, "T")
    feed(detector, [80] * 20)
    feed(detector, [487, 492, 503, 510], start=1100.0, step=0.5)
    incident = detector.flush()

    assert incident is not None
    assert incident.sample_count == 4
    assert incident.peak_latency_ms == 510
    assert incident.duration_s == pytest.approx(1.5)
    assert incident.severity == Severity.SEVERE


def test_spikes_separated_by_a_gap_are_separate_incidents():
    detector = TargetDetector(1, "T", settings=DetectionSettings(incident_gap_seconds=2.0))
    feed(detector, [80] * 20)
    closed = feed(detector, [500], start=1100.0)
    # 10 s later, well past the merge gap.
    closed += feed(detector, [80] * 6 + [500], start=1110.0)
    closed += [i for i in [detector.flush()] if i]
    assert len(closed) == 2


def test_failed_probes_during_an_incident_are_counted():
    detector = TargetDetector(1, "T")
    feed(detector, [80] * 20)
    feed(detector, [500, None, 520], start=1100.0)
    incident = detector.flush()
    assert incident is not None
    assert incident.failed_count == 1
    assert incident.packet_loss > 0


# --------------------------------------------------------------------- loss --


def test_packet_loss_uses_a_sliding_window():
    detector = TargetDetector(1, "T", settings=DetectionSettings(loss_window_samples=10))
    feed(detector, [20] * 8 + [None, None])
    assert detector.loss_fraction == pytest.approx(0.2)

    # Once ten good samples have scrolled past, the window is clean again.
    feed(detector, [20] * 10, start=1010.0)
    assert detector.loss_fraction == 0.0


def test_session_loss_fraction_covers_everything():
    detector = TargetDetector(1, "T", settings=DetectionSettings(loss_window_samples=5))
    feed(detector, [20] * 90 + [None] * 10)
    assert detector.session_loss_fraction == pytest.approx(0.1)


def test_target_becomes_unreachable_after_repeated_failures():
    detector = TargetDetector(1, "T")
    feed(detector, [20] * 10)
    assert detector.reachable

    feed(detector, [None, None, None], start=1100.0)
    assert not detector.reachable
    assert detector.status() == NodeStatus.PROBLEM

    feed(detector, [20], start=1200.0)
    assert detector.reachable


# -------------------------------------------------------------------- status --


def test_gateway_uses_stricter_latency_thresholds():
    gateway = TargetDetector(1, "Router", category=TargetCategory.GATEWAY.value)
    feed(gateway, [30] * 30)  # fine for the internet, bad for a router
    assert gateway.status() in (NodeStatus.WARNING, NodeStatus.PROBLEM)

    internet = TargetDetector(2, "Cloudflare", category=TargetCategory.INTERNET.value)
    feed(internet, [30] * 30)
    assert internet.status() == NodeStatus.HEALTHY


def test_sustained_degradation_is_detected_without_spikes():
    """Plan section 87: a slow ramp never trips the spike rule."""
    detector = TargetDetector(1, "T")
    feed(detector, [20] * 40)
    # 2 ms per sample: slow enough that the rolling median tracks the rise and
    # no individual sample ever looks like a spike, yet the link ends up nearly
    # 9x slower than it started.
    ramp = [20 + i * 2 for i in range(80)]  # 20 ms -> 178 ms
    feed(detector, ramp, start=1100.0)
    assert detector.spike_count == 0
    assert detector.sustained_degradation()
    assert detector.status() != NodeStatus.HEALTHY


def test_stats_percentiles():
    detector = TargetDetector(1, "T")
    feed(detector, list(range(1, 101)))
    stats = detector.stats()
    assert stats.min_ms == 1
    assert stats.max_ms == 100
    assert stats.median_ms == pytest.approx(50.5)
    assert stats.p95_ms == pytest.approx(95, abs=1)
    assert stats.p99_ms == pytest.approx(99, abs=1)


# ------------------------------------------------------------------ registry --


def test_detector_registry_tracks_targets_independently():
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    detector.register(2, "Game", TargetCategory.CUSTOM.value)

    timestamp = 1000.0
    for _ in range(20):
        detector.observe(make_measurement(2.0, timestamp, target_id=1,
                                          category=TargetCategory.GATEWAY.value))
        detector.observe(make_measurement(80.0, timestamp, target_id=2))
        timestamp += 0.5
    detector.observe(make_measurement(600.0, timestamp, target_id=2))

    assert detector.get(1).spike_count == 0
    assert detector.get(2).spike_count == 1
    assert detector.total_spikes() == 1


def test_unregistered_target_is_auto_registered():
    detector = Detector()
    detector.observe(make_measurement(20.0, 1000.0, target_id=99))
    assert detector.get(99) is not None


def test_apply_settings_keeps_history():
    detector = Detector()
    detector.register(1, "T")
    feed(detector.get(1), [20] * 30)
    detector.apply_settings(DetectionSettings(rolling_window=20))
    assert detector.get(1).settings.rolling_window == 20
    assert detector.get(1).total_samples == 30


def test_reset_clears_state():
    detector = TargetDetector(1, "T")
    feed(detector, [20] * 30 + [500])
    detector.reset()
    assert detector.total_samples == 0
    assert detector.spike_count == 0
    assert detector.baseline_ms is None
