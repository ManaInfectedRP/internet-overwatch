"""Diagnosis engine tests: rules A-C, correlation, confidence, offline states.

Covers plan sections 16-19, 60, 68, 70-72 and 97.
"""

from __future__ import annotations

from app.config.defaults import Confidence, NodeStatus, TargetCategory
from app.core.detector import Detector
from app.core.diagnostics import (
    analyse_destination,
    analyse_internet,
    analyse_local,
    build_report,
    correlate_incident,
    diagnose,
    diagnose_incident,
    enrich_incident,
    offline_state,
)
from app.core.simulator import Simulator
from app.storage.models import Measurement, TargetStats


def stats(name, category, average=20.0, loss=0.0, reachable=True,
          status=NodeStatus.HEALTHY, spikes=0, samples=100, jitter=3.0):
    return TargetStats(
        target_id=abs(hash(name)) % 1000,
        target_name=name,
        category=category,
        current_ms=average,
        average_ms=average,
        jitter_ms=jitter,
        loss_fraction=loss,
        sample_count=samples,
        reachable=reachable,
        status=status,
        spike_count=spikes,
    )


HEALTHY_GATEWAY = [stats("Router", TargetCategory.GATEWAY.value, 2.0)]
HEALTHY_INTERNET = [
    stats("Cloudflare", TargetCategory.INTERNET.value, 21.0),
    stats("Google", TargetCategory.INTERNET.value, 24.0),
]


def run_scenario(scenario, ticks=220, seed=1):
    """Drive the simulator through the detector and return (detector, incidents)."""
    simulator = Simulator(scenario=scenario, seed=seed)
    detector = Detector()
    incidents = []
    for measurement in simulator.stream(ticks):
        _, incident = detector.observe(measurement)
        if incident is not None:
            incidents.append(enrich_incident(incident, detector))
    incidents.extend(enrich_incident(i, detector) for i in detector.flush_all())
    return detector, incidents


# ------------------------------------------------------------ layer reports --


def test_local_layer_healthy():
    report = analyse_local(HEALTHY_GATEWAY)
    assert report.status == NodeStatus.HEALTHY
    assert "healthy" in report.summary.lower()
    assert any("Gateway reachable: YES" in line for line in report.lines)


def test_local_layer_flags_slow_gateway():
    report = analyse_local([stats("Router", TargetCategory.GATEWAY.value, 60.0)])
    assert report.status == NodeStatus.PROBLEM


def test_local_layer_flags_a_spiking_router():
    """Plan section 97: a router that spikes is a local problem even if its
    average looks acceptable."""
    report = analyse_local([
        stats("Router", TargetCategory.GATEWAY.value, average=25.0, spikes=12,
              status=NodeStatus.PROBLEM)
    ])
    assert report.status == NodeStatus.PROBLEM


def test_local_layer_unknown_without_a_gateway_target():
    report = analyse_local([])
    assert report.status == NodeStatus.UNKNOWN


def test_internet_layer_flags_several_degraded_targets():
    report = analyse_internet([
        stats("Cloudflare", TargetCategory.INTERNET.value, 300.0,
              status=NodeStatus.PROBLEM),
        stats("Google", TargetCategory.INTERNET.value, 320.0, status=NodeStatus.WARNING),
    ])
    assert report.status == NodeStatus.PROBLEM
    assert "multiple" in report.summary.lower()


def test_internet_layer_tolerates_one_flaky_target():
    report = analyse_internet([
        stats("Cloudflare", TargetCategory.INTERNET.value, 21.0),
        stats("Flaky", TargetCategory.INTERNET.value, 300.0, status=NodeStatus.PROBLEM),
    ])
    assert report.status == NodeStatus.WARNING


def test_destination_layer_without_a_target_says_so():
    report = analyse_destination([])
    assert report.status == NodeStatus.UNKNOWN
    assert any("Targets page" in line for line in report.lines)


# -------------------------------------------------------------- rules A-C ---


def test_rule_a_local_problem():
    local = analyse_local([stats("Router", TargetCategory.GATEWAY.value, 80.0)])
    diagnosis = diagnose(local, analyse_internet(HEALTHY_INTERNET),
                         analyse_destination([]))
    assert diagnosis.rule == "A"
    assert diagnosis.layer == "local"
    assert "local network" in diagnosis.headline.lower()


def test_rule_b_isp_problem():
    internet = analyse_internet([
        stats("Cloudflare", TargetCategory.INTERNET.value, 400.0,
              status=NodeStatus.PROBLEM),
        stats("Google", TargetCategory.INTERNET.value, 420.0, status=NodeStatus.PROBLEM),
    ])
    diagnosis = diagnose(analyse_local(HEALTHY_GATEWAY), internet, analyse_destination([]))
    assert diagnosis.rule == "B"
    assert diagnosis.confidence == Confidence.LIKELY


def test_rule_c_destination_specific():
    destination = analyse_destination([
        stats("Game", TargetCategory.CUSTOM.value, 500.0, status=NodeStatus.PROBLEM)
    ])
    diagnosis = diagnose(analyse_local(HEALTHY_GATEWAY),
                         analyse_internet(HEALTHY_INTERNET), destination)
    assert diagnosis.rule == "C"
    assert diagnosis.layer == "destination"


def test_everything_healthy_claims_nothing():
    diagnosis = diagnose(
        analyse_local(HEALTHY_GATEWAY),
        analyse_internet(HEALTHY_INTERNET),
        analyse_destination([stats("Game", TargetCategory.CUSTOM.value, 80.0)]),
    )
    assert diagnosis.confidence == Confidence.UNCLEAR
    assert diagnosis.headline == "No clear cause detected"


def test_diagnosis_never_asserts_certainty():
    """Plan section 16: wording is graded, never absolute."""
    for scenario in ("router_spikes", "isp_spikes", "destination_spikes",
                     "packet_loss", "stable"):
        detector, _ = run_scenario(scenario, ticks=180)
        diagnosis = build_report(detector).diagnosis
        assert diagnosis.confidence in Confidence
        assert "definitely" not in diagnosis.wording.lower()
        assert diagnosis.wording.startswith(
            ("Likely cause", "Possible cause", "No clear cause", "Mild instability")
        )


# ------------------------------------------------- end-to-end rule mapping ---


def test_scenarios_map_to_the_expected_layer():
    expected = {
        "router_spikes": "local",
        "isp_spikes": "internet",
        "destination_spikes": "destination",
    }
    for scenario, layer in expected.items():
        detector, _ = run_scenario(scenario)
        diagnosis = build_report(detector).diagnosis
        assert diagnosis.layer == layer, f"{scenario} -> {diagnosis.layer}"
        assert diagnosis.confidence == Confidence.LIKELY


def test_stable_scenario_produces_no_incidents():
    detector, incidents = run_scenario("stable")
    assert detector.total_spikes() == 0
    assert incidents == []


# ------------------------------------------------------------- correlation --


def test_correlation_identifies_a_destination_only_spike():
    """Plan section 70's worked example."""
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    detector.register(2, "Cloudflare", TargetCategory.INTERNET.value)
    detector.register(3, "Game", TargetCategory.CUSTOM.value)

    timestamp = 1000.0
    incidents = []
    for index in range(40):
        game = 490.0 if 20 <= index < 24 else 80.0
        detector.observe(Measurement(1, "Router", TargetCategory.GATEWAY.value,
                                     timestamp, True, 2.0))
        detector.observe(Measurement(2, "Cloudflare", TargetCategory.INTERNET.value,
                                     timestamp, True, 21.0))
        # The incident closes once the calm samples exceed the merge gap, so it
        # is returned here rather than by flush_all().
        _, closed = detector.observe(
            Measurement(3, "Game", TargetCategory.CUSTOM.value, timestamp, True, game)
        )
        if closed is not None:
            incidents.append(enrich_incident(closed, detector))
        timestamp += 0.5

    incidents.extend(enrich_incident(i, detector) for i in detector.flush_all())
    assert len(incidents) == 1
    incident = incidents[0]

    assert incident.correlation["gateway_stable"] is True
    assert incident.correlation["internet_stable"] is True
    assert incident.diagnosis == "Destination-specific instability"
    assert incident.confidence == Confidence.LIKELY
    assert any("gateway remained stable" in e.lower() for e in incident.evidence)


def test_correlation_identifies_a_local_spike():
    detector, incidents = run_scenario("router_spikes")
    assert incidents
    local = [i for i in incidents if i.category == TargetCategory.GATEWAY.value]
    assert local, "the router itself should have recorded incidents"
    assert all(i.correlation.get("gateway_stable") in (None, False) or
               i.diagnosis for i in incidents)
    # Downstream targets should be attributed to the local layer.
    downstream = [i for i in incidents if i.category != TargetCategory.GATEWAY.value]
    assert any(i.diagnosis == "Local network instability" for i in downstream)


def test_incident_without_comparable_data_stays_unclear():
    detector = Detector()
    detector.register(1, "Only target", TargetCategory.CUSTOM.value)
    timestamp = 1000.0
    for index in range(30):
        value = 500.0 if index >= 25 else 50.0
        detector.observe(Measurement(1, "Only target", TargetCategory.CUSTOM.value,
                                     timestamp, True, value))
        timestamp += 0.5
    incidents = [enrich_incident(i, detector) for i in detector.flush_all()]
    assert incidents
    assert incidents[0].confidence == Confidence.UNCLEAR


def test_correlation_window_is_bounded():
    """Samples far from the incident must not be pulled into the comparison."""
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    detector.register(2, "Game", TargetCategory.CUSTOM.value)
    for index in range(30):
        detector.observe(Measurement(1, "Router", TargetCategory.GATEWAY.value,
                                     1000.0 + index * 0.5, True, 2.0))
    for index in range(30):
        detector.observe(Measurement(2, "Game", TargetCategory.CUSTOM.value,
                                     5000.0 + index * 0.5, True,
                                     500.0 if index >= 25 else 50.0))
    incidents = detector.flush_all()
    correlation = correlate_incident(incidents[0], detector, window_s=2.0)
    assert correlation["entries"] == []


# ---------------------------------------------------------- offline states --


def test_offline_state_local_network_down():
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    for index in range(5):
        detector.observe(Measurement(1, "Router", TargetCategory.GATEWAY.value,
                                     1000.0 + index, False, None, "timeout"))
    assert offline_state(detector) == "LOCAL NETWORK UNREACHABLE"


def test_offline_state_internet_down_but_router_up():
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    detector.register(2, "Cloudflare", TargetCategory.INTERNET.value)
    for index in range(5):
        detector.observe(Measurement(1, "Router", TargetCategory.GATEWAY.value,
                                     1000.0 + index, True, 2.0))
        detector.observe(Measurement(2, "Cloudflare", TargetCategory.INTERNET.value,
                                     1000.0 + index, False, None, "timeout"))
    state = offline_state(detector)
    assert state is not None and "INTERNET UNREACHABLE" in state


def test_offline_state_single_target_down():
    detector = Detector()
    detector.register(1, "Router", TargetCategory.GATEWAY.value)
    detector.register(2, "Cloudflare", TargetCategory.INTERNET.value)
    detector.register(3, "Game", TargetCategory.CUSTOM.value)
    for index in range(5):
        detector.observe(Measurement(1, "Router", TargetCategory.GATEWAY.value,
                                     1000.0 + index, True, 2.0))
        detector.observe(Measurement(2, "Cloudflare", TargetCategory.INTERNET.value,
                                     1000.0 + index, True, 21.0))
        detector.observe(Measurement(3, "Game", TargetCategory.CUSTOM.value,
                                     1000.0 + index, False, None, "timeout"))
    state = offline_state(detector)
    assert state is not None and state.startswith("TARGET UNREACHABLE")


def test_offline_state_none_when_everything_works():
    detector, _ = run_scenario("stable", ticks=40)
    assert offline_state(detector) is None


def test_complete_outage_is_reported_as_local():
    detector, _ = run_scenario("complete_outage", ticks=60)
    report = build_report(detector)
    assert report.local.status == NodeStatus.PROBLEM
    assert report.diagnosis.layer == "local"
