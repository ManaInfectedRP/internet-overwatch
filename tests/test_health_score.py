"""Health score tests (plan sections 8.1, 67)."""

from __future__ import annotations

import pytest

from app.config.defaults import HealthStatus, NodeStatus, TargetCategory
from app.core.health_score import (
    compute_health,
    jitter_score,
    latency_score,
    linear_score,
    local_network_score,
    loss_score,
    overall_node_status,
    spike_score,
    status_for_score,
)
from app.storage.models import TargetStats


def stats(
    name="Game",
    category=TargetCategory.CUSTOM.value,
    average=40.0,
    jitter=5.0,
    loss=0.0,
    samples=100,
    reachable=True,
    status=NodeStatus.HEALTHY,
    target_id=1,
):
    return TargetStats(
        target_id=target_id,
        target_name=name,
        category=category,
        current_ms=average,
        average_ms=average,
        jitter_ms=jitter,
        loss_fraction=loss,
        sample_count=samples,
        failed_count=int(samples * loss),
        reachable=reachable,
        status=status,
    )


# ------------------------------------------------------------- components ---


def test_linear_score_endpoints_and_midpoint():
    assert linear_score(10, 10, 20) == 100
    assert linear_score(20, 10, 20) == 0
    assert linear_score(15, 10, 20) == pytest.approx(50)
    assert linear_score(5, 10, 20) == 100
    assert linear_score(999, 10, 20) == 0


def test_linear_score_treats_missing_data_as_neutral():
    assert linear_score(None, 10, 20) == 100


def test_component_scores_move_in_the_right_direction():
    assert latency_score(20) > latency_score(120) > latency_score(400)
    assert jitter_score(2) > jitter_score(30) > jitter_score(200)
    assert loss_score(0.0) > loss_score(0.01) > loss_score(0.5)
    assert spike_score(0, 10) > spike_score(5, 10) > spike_score(200, 10)


def test_spike_score_is_rate_based_not_count_based():
    """20 spikes in an hour is not the same as 20 in a minute."""
    assert spike_score(20, 60) > spike_score(20, 1)


def test_local_network_score_penalises_a_bad_gateway():
    good = stats(category=TargetCategory.GATEWAY.value, average=2.0)
    bad = stats(category=TargetCategory.GATEWAY.value, average=45.0)
    down = stats(category=TargetCategory.GATEWAY.value, reachable=False)

    assert local_network_score(good) == 100
    assert local_network_score(bad) < 50
    assert local_network_score(down) == 0
    assert local_network_score(None) == 100


def test_status_bands():
    assert status_for_score(95) == HealthStatus.EXCELLENT
    assert status_for_score(85) == HealthStatus.GOOD
    assert status_for_score(70) == HealthStatus.STABLE
    assert status_for_score(55) == HealthStatus.UNSTABLE
    assert status_for_score(35) == HealthStatus.POOR
    assert status_for_score(5) == HealthStatus.CRITICAL


# ----------------------------------------------------------------- blend ---


def test_perfect_connection_scores_high():
    result = compute_health(
        {
            1: stats(category=TargetCategory.GATEWAY.value, average=1.5, jitter=0.5),
            2: stats(name="Game", average=25.0, jitter=2.0),
        },
        spike_count=0,
        duration_minutes=10,
    )
    assert result.score >= 90
    assert result.status == HealthStatus.EXCELLENT
    assert result.node_status == NodeStatus.HEALTHY


def test_bad_connection_scores_low():
    result = compute_health(
        {
            1: stats(category=TargetCategory.GATEWAY.value, average=30.0, loss=0.03),
            2: stats(name="Game", average=350.0, jitter=90.0, loss=0.08),
        },
        spike_count=60,
        duration_minutes=10,
    )
    assert result.score < 40
    assert result.status in (HealthStatus.POOR, HealthStatus.CRITICAL)


def test_no_measurements_yields_zero_and_says_so():
    result = compute_health({}, 0, 1)
    assert result.score == 0
    assert "No measurements" in result.reasons[0]


def test_unreachable_gateway_caps_the_score():
    """A dead router cannot be averaged away by healthy public targets."""
    result = compute_health(
        {
            1: stats(category=TargetCategory.GATEWAY.value, reachable=False,
                     status=NodeStatus.PROBLEM),
            2: stats(name="Cloudflare", category=TargetCategory.INTERNET.value,
                     average=20.0),
        },
        spike_count=0,
        duration_minutes=10,
    )
    assert result.score <= 10
    assert any("gateway" in reason.lower() for reason in result.reasons)


def test_all_targets_unreachable_is_near_zero():
    result = compute_health(
        {
            1: stats(category=TargetCategory.GATEWAY.value, reachable=False),
            2: stats(name="Cloudflare", category=TargetCategory.INTERNET.value,
                     reachable=False),
        },
        spike_count=0,
        duration_minutes=10,
    )
    assert result.score <= 10


def test_custom_target_dominates_over_internet_targets():
    """The user cares about the game server, not about 1.1.1.1."""
    internet_only = compute_health(
        {1: stats(name="Cloudflare", category=TargetCategory.INTERNET.value, average=20.0)},
        0, 10,
    )
    with_bad_game = compute_health(
        {
            1: stats(name="Cloudflare", category=TargetCategory.INTERNET.value,
                     average=20.0),
            2: stats(name="Game", average=400.0, jitter=80.0),
        },
        0, 10,
    )
    assert with_bad_game.score < internet_only.score


def test_best_internet_target_represents_the_layer():
    """One flaky public target must not condemn the whole connection."""
    result = compute_health(
        {
            1: stats(name="Good", category=TargetCategory.INTERNET.value, average=20.0),
            2: stats(name="Flaky", category=TargetCategory.INTERNET.value,
                     average=300.0, loss=0.2),
        },
        0, 10,
    )
    assert result.score > 50


def test_components_and_weights_are_reported():
    result = compute_health({1: stats()}, 2, 10)
    assert set(result.components) == {"latency", "jitter", "loss", "spikes", "local"}
    assert result.weights
    assert result.reasons


def test_overall_node_status_is_the_worst_target():
    assert overall_node_status({
        1: stats(status=NodeStatus.HEALTHY),
        2: stats(status=NodeStatus.WARNING, target_id=2),
        3: stats(status=NodeStatus.PROBLEM, target_id=3),
    }) == NodeStatus.PROBLEM
    assert overall_node_status({}) == NodeStatus.UNKNOWN
