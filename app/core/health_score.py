"""Health score model (plan sections 8.1, 67).

The score is a weighted blend of five component scores, each normalised to
0-100 by linear interpolation between a "good" and a "bad" reference point.
Weights and reference points live in `app.config.defaults` so the model can be
tuned in one place.

This is deliberately a user-friendly indicator, not a scientific measure, and
the UI says so.
"""

from __future__ import annotations

from app.config.defaults import (
    HEALTH_BANDS,
    HEALTH_JITTER_BAD_MS,
    HEALTH_JITTER_GOOD_MS,
    HEALTH_LATENCY_BAD_MS,
    HEALTH_LATENCY_GOOD_MS,
    HEALTH_LOSS_BAD,
    HEALTH_SPIKES_PER_MIN_BAD,
    HEALTH_WEIGHTS,
    HealthStatus,
    NodeStatus,
    TargetCategory,
)
from app.storage.models import HealthResult, TargetStats


def linear_score(value: float | None, good: float, bad: float) -> float:
    """100 at or below `good`, 0 at or above `bad`, linear in between."""
    if value is None:
        return 100.0
    if bad <= good:  # pragma: no cover - guards a misconfiguration
        return 100.0 if value <= good else 0.0
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (1.0 - (value - good) / (bad - good))


def status_for_score(score: float) -> HealthStatus:
    for threshold, status in HEALTH_BANDS:
        if score >= threshold:
            return status
    return HealthStatus.CRITICAL  # pragma: no cover - last band is 0


def latency_score(latency_ms: float | None) -> float:
    return linear_score(latency_ms, HEALTH_LATENCY_GOOD_MS, HEALTH_LATENCY_BAD_MS)


def jitter_score(jitter_ms: float | None) -> float:
    return linear_score(jitter_ms, HEALTH_JITTER_GOOD_MS, HEALTH_JITTER_BAD_MS)


def loss_score(loss_fraction: float | None) -> float:
    return linear_score(loss_fraction, 0.0, HEALTH_LOSS_BAD)


def spike_score(spikes: int, minutes: float) -> float:
    if minutes <= 0:
        return 100.0
    per_minute = spikes / minutes
    return linear_score(per_minute, 0.0, HEALTH_SPIKES_PER_MIN_BAD)


def local_network_score(gateway_stats: TargetStats | None) -> float:
    """Local layer health: gateway latency and loss (plan section 17)."""
    if gateway_stats is None or gateway_stats.sample_count == 0:
        return 100.0
    if not gateway_stats.reachable:
        return 0.0
    latency = linear_score(gateway_stats.average_ms, 3.0, 50.0)
    loss = linear_score(gateway_stats.loss_fraction, 0.0, 0.02)
    return min(latency, loss)


def _primary_stats(stats: dict[int | None, TargetStats]) -> TargetStats | None:
    """Pick the target the score should be dominated by.

    A custom/game target is what the user actually cares about; if none is
    configured, fall back to the internet layer.
    """
    customs = [s for s in stats.values() if s.category == TargetCategory.CUSTOM.value
               and s.sample_count]
    if customs:
        return max(customs, key=lambda s: s.sample_count)
    internets = [s for s in stats.values() if s.category == TargetCategory.INTERNET.value
                 and s.sample_count]
    if internets:
        # The best-performing public target represents the internet path;
        # one flaky target should not condemn the whole connection.
        return min(internets, key=lambda s: (s.loss_fraction, s.average_ms or 1e9))
    populated = [s for s in stats.values() if s.sample_count]
    return populated[0] if populated else None


def compute_health(
    stats: dict[int | None, TargetStats],
    spike_count: int = 0,
    duration_minutes: float = 1.0,
    weights: dict[str, float] | None = None,
) -> HealthResult:
    """Blend the component scores into the headline 0-100 number."""
    weights = weights or HEALTH_WEIGHTS
    primary = _primary_stats(stats)
    gateway = next(
        (s for s in stats.values() if s.category == TargetCategory.GATEWAY.value), None
    )

    if primary is None:
        return HealthResult(
            score=0,
            status=HealthStatus.CRITICAL,
            components={},
            weights=dict(weights),
            reasons=["No measurements yet"],
        )

    latency_reference = primary.average_ms
    components = {
        "latency": latency_score(latency_reference),
        "jitter": jitter_score(primary.jitter_ms),
        "loss": loss_score(primary.loss_fraction),
        "spikes": spike_score(spike_count, max(0.1, duration_minutes)),
        "local": local_network_score(gateway),
    }

    total_weight = sum(weights.get(key, 0.0) for key in components)
    if total_weight <= 0:  # pragma: no cover - guards a misconfiguration
        total_weight = 1.0
    score = sum(components[key] * weights.get(key, 0.0) for key in components) / total_weight

    # An unreachable target is a hard failure, not something to average away.
    unreachable = [s for s in stats.values() if s.sample_count and not s.reachable]
    reasons: list[str] = []
    if unreachable:
        names = ", ".join(s.target_name for s in unreachable)
        if all(s.category == TargetCategory.GATEWAY.value for s in unreachable):
            score = min(score, 10.0)
            reasons.append(f"Local gateway unreachable ({names})")
        elif len(unreachable) == len([s for s in stats.values() if s.sample_count]):
            score = min(score, 5.0)
            reasons.append("All monitored targets unreachable")
        else:
            score = min(score, 40.0)
            reasons.append(f"Target unreachable: {names}")

    for key, label, threshold in (
        ("latency", "Latency is elevated", 70),
        ("jitter", "Jitter is high", 70),
        ("loss", "Packet loss detected", 90),
        ("spikes", "Frequent lag spikes", 70),
        ("local", "Local network is degraded", 70),
    ):
        if components[key] < threshold:
            reasons.append(label)

    score = max(0.0, min(100.0, score))
    return HealthResult(
        score=int(round(score)),
        status=status_for_score(score),
        components=components,
        weights=dict(weights),
        reasons=reasons or ["All monitored layers look healthy"],
    )


def overall_node_status(stats: dict[int | None, TargetStats]) -> NodeStatus:
    """Worst status across all targets (plan section 72)."""
    statuses = [s.status for s in stats.values() if s.sample_count]
    if not statuses:
        return NodeStatus.UNKNOWN
    return max(statuses, key=lambda s: s.rank)
