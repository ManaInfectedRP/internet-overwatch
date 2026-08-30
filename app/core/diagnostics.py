"""Automatic diagnosis engine (plan sections 15-19, 68, 70-72).

Two related jobs live here:

* **Layer analysis** - summarise the local, internet and destination layers and
  apply rules A-C to say which layer is most likely responsible.
* **Spike correlation** - for one incident, compare every other target inside a
  short window around it (plan section 71) and decide whether the problem was
  local, upstream or destination-specific.

The engine never claims certainty it does not have: every conclusion carries a
:class:`Confidence` and the wording is graded accordingly (plan section 16).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.defaults import (
    CORRELATION_WINDOW_S,
    GATEWAY_LATENCY_PROBLEM_MS,
    GATEWAY_LATENCY_WARNING_MS,
    Confidence,
    NodeStatus,
    Severity,
    TargetCategory,
)
from app.config.settings import DetectionSettings
from app.core.detector import Detector, TargetDetector, classify_jitter, classify_loss
from app.storage.models import Diagnosis, Incident, TargetStats
from app.utils.logger import get_logger
from app.utils.time import format_latency

log = get_logger("core.diagnostics")


@dataclass
class LayerReport:
    """One section of the Diagnostics page (plan section 15)."""

    name: str
    status: NodeStatus = NodeStatus.UNKNOWN
    summary: str = ""
    lines: list[str] = field(default_factory=list)
    stats: list[TargetStats] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.status == NodeStatus.HEALTHY

    @property
    def degraded(self) -> bool:
        return self.status in (NodeStatus.WARNING, NodeStatus.PROBLEM)


@dataclass
class DiagnosticReport:
    """Everything the Diagnostics page and the ISP report need."""

    local: LayerReport
    internet: LayerReport
    destination: LayerReport
    diagnosis: Diagnosis

    @property
    def layers(self) -> list[LayerReport]:
        return [self.local, self.internet, self.destination]


def _worst_status(statuses: list[NodeStatus]) -> NodeStatus:
    known = [s for s in statuses if s != NodeStatus.UNKNOWN]
    if not known:
        return NodeStatus.UNKNOWN
    return max(known, key=lambda s: s.rank)


def _describe_target(stats: TargetStats) -> str:
    if not stats.sample_count:
        return f"{stats.target_name}: no samples yet"
    if not stats.reachable:
        return f"{stats.target_name}: unreachable"
    parts = [f"{stats.target_name}: {format_latency(stats.average_ms)} avg"]
    if stats.jitter_ms is not None:
        parts.append(f"jitter {format_latency(stats.jitter_ms)}")
    parts.append(f"loss {stats.loss_percent:.1f}%")
    if stats.spike_count:
        parts.append(f"{stats.spike_count} spikes")
    return ", ".join(parts)


def analyse_local(
    gateway_stats: list[TargetStats], settings: DetectionSettings | None = None
) -> LayerReport:
    """Rule A - is the local network healthy? (plan section 17)"""
    settings = settings or DetectionSettings()
    report = LayerReport(name="Local Network", stats=gateway_stats)
    active = [s for s in gateway_stats if s.sample_count]
    if not active:
        report.status = NodeStatus.UNKNOWN
        report.summary = "No gateway is being monitored"
        report.lines.append("Add a gateway target to check the local network.")
        return report

    primary = active[0]
    report.lines.append(f"Gateway reachable: {'YES' if primary.reachable else 'NO'}")
    report.lines.append(f"Gateway latency: {format_latency(primary.average_ms)}")
    report.lines.append(f"Gateway loss: {primary.loss_percent:.1f}%")
    if primary.jitter_ms is not None:
        report.lines.append(f"Gateway jitter: {format_latency(primary.jitter_ms)}")

    if not primary.reachable:
        report.status = NodeStatus.PROBLEM
        report.summary = "Local network unreachable"
        return report

    if primary.spike_count:
        report.lines.append(f"Gateway spikes: {primary.spike_count}")

    latency = primary.average_ms or 0.0
    loss = primary.loss_fraction
    # A router that spikes is a local problem even when its average looks fine,
    # which is exactly the comparison plan section 97 asks for.
    spiking = primary.spike_count > 0 and primary.status == NodeStatus.PROBLEM
    if latency >= GATEWAY_LATENCY_PROBLEM_MS or loss >= settings.loss_warning or spiking:
        report.status = NodeStatus.PROBLEM
        report.summary = "Possible local network issue"
    elif (
        latency >= GATEWAY_LATENCY_WARNING_MS
        or loss > 0
        or primary.status == NodeStatus.WARNING
    ):
        report.status = NodeStatus.WARNING
        report.summary = "Local network shows mild degradation"
    else:
        report.status = NodeStatus.HEALTHY
        report.summary = "Local network appears healthy"
    return report


def analyse_internet(
    internet_stats: list[TargetStats], settings: DetectionSettings | None = None
) -> LayerReport:
    """Rule B - are the public targets healthy? (plan section 18)"""
    settings = settings or DetectionSettings()
    report = LayerReport(name="Internet", stats=internet_stats)
    active = [s for s in internet_stats if s.sample_count]
    if not active:
        report.status = NodeStatus.UNKNOWN
        report.summary = "No public targets are being monitored"
        return report

    for stats in active:
        report.lines.append(_describe_target(stats))

    unreachable = [s for s in active if not s.reachable]
    degraded = [s for s in active if s.status in (NodeStatus.WARNING, NodeStatus.PROBLEM)]

    if len(unreachable) == len(active):
        report.status = NodeStatus.PROBLEM
        report.summary = "Internet unreachable"
    elif len(degraded) >= 2:
        # Independent public targets degrading together is the signature of an
        # upstream problem rather than a coincidence (plan section 18).
        report.status = NodeStatus.PROBLEM
        report.summary = "Multiple public targets are degraded"
    elif len(active) == 1 and degraded:
        report.status = degraded[0].status
        report.summary = "The only public target is degraded"
    elif degraded:
        report.status = NodeStatus.WARNING
        report.summary = "One public target is degraded"
    else:
        report.status = NodeStatus.HEALTHY
        report.summary = "Internet connection appears healthy"
    return report


def analyse_destination(
    custom_stats: list[TargetStats], settings: DetectionSettings | None = None
) -> LayerReport:
    """Rule C - is the game/custom destination healthy? (plan section 19)"""
    settings = settings or DetectionSettings()
    report = LayerReport(name="Destination", stats=custom_stats)
    active = [s for s in custom_stats if s.sample_count]
    if not active:
        report.status = NodeStatus.UNKNOWN
        report.summary = "No custom or game target is configured"
        report.lines.append("Add your game server on the Targets page for a full picture.")
        return report

    for stats in active:
        report.lines.append(_describe_target(stats))
        jitter_label, _ = classify_jitter(stats.jitter_ms)
        loss_label, _ = classify_loss(stats.loss_fraction, settings)
        report.lines.append(f"   jitter: {jitter_label}, loss: {loss_label}")

    report.status = _worst_status([s.status for s in active])
    if report.status == NodeStatus.PROBLEM:
        report.summary = "Destination path appears unstable"
    elif report.status == NodeStatus.WARNING:
        report.summary = "Destination path shows mild instability"
    else:
        report.summary = "Destination path appears healthy"
    return report


def diagnose(
    local: LayerReport,
    internet: LayerReport,
    destination: LayerReport,
    spike_count: int = 0,
) -> Diagnosis:
    """Apply rules A-C across the three layers (plan sections 17-19)."""
    evidence: list[str] = []

    # Rule A: the local layer is degraded, so everything downstream inherits it.
    if local.status == NodeStatus.PROBLEM:
        evidence.append(f"Local: {local.summary.lower()}")
        if internet.degraded:
            evidence.append("Public targets are degraded at the same time")
        confidence = Confidence.LIKELY if internet.degraded else Confidence.POSSIBLE
        return Diagnosis(
            headline="Local network issue",
            detail=(
                "The gateway itself is slow, lossy or unreachable. Wi-Fi interference, "
                "a weak signal, router load, LAN congestion or an adapter problem are "
                "the usual explanations."
            ),
            confidence=confidence,
            evidence=evidence,
            rule="A",
            layer="local",
        )

    # Rule B: gateway fine, several public targets bad.
    if local.status in (NodeStatus.HEALTHY, NodeStatus.UNKNOWN, NodeStatus.WARNING) and (
        internet.status == NodeStatus.PROBLEM
    ):
        evidence.append(f"Local: {local.summary.lower()}")
        evidence.append(f"Internet: {internet.summary.lower()}")
        confidence = (
            Confidence.LIKELY if local.status == NodeStatus.HEALTHY else Confidence.POSSIBLE
        )
        return Diagnosis(
            headline="ISP or internet path issue",
            detail=(
                "The local network looks fine while several independent public targets "
                "are degraded at the same time, which points upstream rather than at "
                "your own equipment."
            ),
            confidence=confidence,
            evidence=evidence,
            rule="B",
            layer="internet",
        )

    # Rule C: gateway and public targets fine, destination bad.
    if destination.degraded and not internet.degraded and not local.degraded:
        evidence.append("Local gateway is stable")
        evidence.append("Public internet targets are stable")
        evidence.append(f"Destination: {destination.summary.lower()}")
        confidence = (
            Confidence.LIKELY if destination.status == NodeStatus.PROBLEM
            else Confidence.POSSIBLE
        )
        return Diagnosis(
            headline="Destination-specific instability",
            detail=(
                "Only the game/custom target is affected. This usually means the route "
                "to that destination, or the destination itself, is the problem rather "
                "than your connection as a whole."
            ),
            confidence=confidence,
            evidence=evidence,
            rule="C",
            layer="destination",
        )

    if internet.status == NodeStatus.WARNING or destination.status == NodeStatus.WARNING:
        evidence.append(f"Internet: {internet.summary.lower()}")
        evidence.append(f"Destination: {destination.summary.lower()}")
        return Diagnosis(
            headline="Mild instability without a clear single cause",
            detail=(
                "Some degradation is visible but it is not consistently isolated to one "
                "layer. Keep monitoring - a longer sample usually makes the pattern clear."
            ),
            confidence=Confidence.UNCLEAR,
            evidence=evidence,
            rule="D",
            layer="mixed",
        )

    if spike_count:
        evidence.append(f"{spike_count} spikes recorded, but all layers are currently healthy")
    return Diagnosis(
        headline="No clear cause detected",
        detail="All monitored layers currently look healthy.",
        confidence=Confidence.UNCLEAR,
        evidence=evidence,
        rule="-",
        layer="none",
    )


def build_report(
    detector: Detector, settings: DetectionSettings | None = None, window: int | None = None
) -> DiagnosticReport:
    """Full three-layer report from live detector state."""
    settings = settings or DetectionSettings()
    stats = detector.stats(window)

    def for_category(category: TargetCategory) -> list[TargetStats]:
        return [s for s in stats.values() if s.category == category.value]

    local = analyse_local(for_category(TargetCategory.GATEWAY), settings)
    internet = analyse_internet(for_category(TargetCategory.INTERNET), settings)
    destination = analyse_destination(for_category(TargetCategory.CUSTOM), settings)
    diagnosis = diagnose(local, internet, destination, detector.total_spikes())
    return DiagnosticReport(local, internet, destination, diagnosis)


# ---------------------------------------------------------------------------
# Spike correlation (plan sections 70-71)
# ---------------------------------------------------------------------------


@dataclass
class CorrelationEntry:
    target_name: str
    category: str
    samples: int
    average_ms: float | None
    failures: int
    spiked: bool

    @property
    def stable(self) -> bool:
        return not self.spiked and self.failures == 0


def correlate_incident(
    incident: Incident,
    detector: Detector,
    window_s: float = CORRELATION_WINDOW_S,
) -> dict:
    """Compare every other target within +/- window of the incident.

    The result is the evidence behind the incident's diagnosis: did the router
    spike at the same moment, or only the destination? (plan section 97)
    """
    start = incident.start - window_s
    end = incident.end + window_s
    entries: list[CorrelationEntry] = []

    for target_detector in detector.targets.values():
        if target_detector.target_id == incident.target_id:
            continue
        measurements = target_detector.measurements_between(start, end)
        if not measurements:
            continue
        latencies = [m.latency_ms for m in measurements
                     if m.success and m.latency_ms is not None]
        entries.append(
            CorrelationEntry(
                target_name=target_detector.target_name,
                category=target_detector.category,
                samples=len(measurements),
                average_ms=sum(latencies) / len(latencies) if latencies else None,
                failures=sum(1 for m in measurements if not m.success),
                spiked=any(m.is_spike for m in measurements),
            )
        )

    return {
        "window_s": window_s,
        "entries": [entry.__dict__ for entry in entries],
        "gateway_stable": all(
            e.stable for e in entries if e.category == TargetCategory.GATEWAY.value
        ) if any(e.category == TargetCategory.GATEWAY.value for e in entries) else None,
        "internet_stable": all(
            e.stable for e in entries if e.category == TargetCategory.INTERNET.value
        ) if any(e.category == TargetCategory.INTERNET.value for e in entries) else None,
        "affected": [e.target_name for e in entries if not e.stable],
    }


def diagnose_incident(incident: Incident, correlation: dict) -> Diagnosis:
    """Turn correlation data into a per-incident diagnosis (plan section 70)."""
    gateway_stable = correlation.get("gateway_stable")
    internet_stable = correlation.get("internet_stable")
    evidence: list[str] = []

    if gateway_stable is True:
        evidence.append("Local gateway remained stable during the incident")
    elif gateway_stable is False:
        evidence.append("Local gateway was also affected during the incident")
    if internet_stable is True:
        evidence.append("Public internet targets remained stable during the incident")
    elif internet_stable is False:
        evidence.append("Public internet targets were also affected during the incident")

    peak = format_latency(incident.peak_latency_ms)
    base = format_latency(incident.baseline_latency_ms)
    evidence.append(f"{incident.target_name}: baseline {base}, peak {peak}")

    if gateway_stable is False:
        return Diagnosis(
            headline="Local network instability",
            detail="The router spiked at the same moment, so the problem starts locally.",
            confidence=Confidence.LIKELY,
            evidence=evidence,
            rule="A",
            layer="local",
        )
    if gateway_stable is True and internet_stable is False:
        return Diagnosis(
            headline="ISP or internet path instability",
            detail=(
                "The router was stable while public targets degraded together, which "
                "points upstream of your equipment."
            ),
            confidence=Confidence.LIKELY,
            evidence=evidence,
            rule="B",
            layer="internet",
        )
    if gateway_stable is True and internet_stable is True:
        if incident.category == TargetCategory.GATEWAY.value:
            layer, headline = "local", "Local network instability"
        elif incident.category == TargetCategory.INTERNET.value:
            layer, headline = "internet", "Single public target instability"
        else:
            layer, headline = "destination", "Destination-specific instability"
        return Diagnosis(
            headline=headline,
            detail=(
                "Everything else stayed healthy while this target spiked, so the "
                "problem looks specific to this destination or its route."
            ),
            confidence=Confidence.LIKELY,
            evidence=evidence,
            rule="C",
            layer=layer,
        )

    return Diagnosis(
        headline="Cause unclear",
        detail=(
            "Not enough comparable samples from the other targets inside the "
            "correlation window to attribute this incident to a layer."
        ),
        confidence=Confidence.UNCLEAR,
        evidence=evidence,
        rule="-",
        layer="unknown",
    )


def enrich_incident(incident: Incident, detector: Detector,
                    window_s: float = CORRELATION_WINDOW_S) -> Incident:
    """Attach correlation results and a diagnosis to a closed incident."""
    correlation = correlate_incident(incident, detector, window_s)
    diagnosis = diagnose_incident(incident, correlation)
    incident.correlation = correlation
    incident.diagnosis = diagnosis.headline
    incident.confidence = diagnosis.confidence
    incident.evidence = diagnosis.evidence
    affected = correlation.get("affected") or []
    incident.targets_affected = [incident.target_name] + [
        name for name in affected if name != incident.target_name
    ]
    return incident


def offline_state(detector: Detector) -> str | None:
    """Connectivity headline for the offline cases in plan section 60."""
    gateways = detector.by_category(TargetCategory.GATEWAY.value)
    internets = detector.by_category(TargetCategory.INTERNET.value)
    customs = detector.by_category(TargetCategory.CUSTOM.value)

    def all_down(detectors: list[TargetDetector]) -> bool:
        active = [d for d in detectors if d.total_samples]
        return bool(active) and all(not d.reachable for d in active)

    if all_down(gateways):
        return "LOCAL NETWORK UNREACHABLE"
    if all_down(internets) and all_down(customs or internets):
        return "LOCAL NETWORK OK - INTERNET UNREACHABLE"
    if all_down(internets):
        return "INTERNET UNREACHABLE"
    down_customs = [d for d in customs if d.total_samples and not d.reachable]
    if down_customs:
        names = ", ".join(d.target_name for d in down_customs)
        return f"TARGET UNREACHABLE: {names}"
    return None


def severity_summary(incidents: list[Incident]) -> dict[Severity, int]:
    counts = {severity: 0 for severity in Severity}
    for incident in incidents:
        counts[incident.severity] += 1
    return counts
