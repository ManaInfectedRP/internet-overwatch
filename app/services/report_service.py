"""Human-readable diagnostic reports (plan sections 42, 69).

The output is deliberately plain text: it should paste cleanly into an ISP
support ticket or a game-support form. Every claim in the report is backed by
a number that also appears in the report, and conclusions keep their
confidence wording.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.config.defaults import APP_NAME, Confidence, NodeStatus, Severity, TargetCategory
from app.core.diagnostics import DiagnosticReport, build_report
from app.core.health_score import compute_health
from app.storage.models import Incident, Session, TargetStats
from app.utils.logger import get_logger
from app.utils.platform import platform_name
from app.utils.time import (
    format_clock,
    format_datetime,
    format_duration,
    format_latency,
    now_ts,
)

log = get_logger("services.report")

DIVIDER = "=" * 68
SUBDIVIDER = "-" * 68


@dataclass
class ReportContext:
    """Everything a report needs, gathered from either live or stored data."""

    session: Session | None = None
    stats: dict[int | None, TargetStats] = field(default_factory=dict)
    incidents: list[Incident] = field(default_factory=list)
    diagnostic: DiagnosticReport | None = None
    health_score: int = 0
    health_status: str = ""
    uptime_s: float = 0.0
    traceroutes: list[dict] = field(default_factory=list)
    network_info: dict = field(default_factory=dict)
    wifi_info: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def context_from_monitor(monitor) -> ReportContext:
    """Build a report context from a live :class:`Monitor`."""
    health = monitor.health()
    return ReportContext(
        session=monitor.session,
        stats=monitor.stats(),
        incidents=monitor.recent_incidents(200),
        diagnostic=build_report(monitor.detector, monitor.settings.detection),
        health_score=health.score,
        health_status=health.status.label,
        uptime_s=monitor.uptime_s,
    )


def context_from_session(repository, session_id: int) -> ReportContext:
    """Build a report context from stored data for a past session."""
    session = repository.get_session(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    stats: dict[int | None, TargetStats] = {}
    for target_id in repository.session_target_ids(session_id):
        stats[target_id] = repository.target_stats(target_id, session_id)

    incidents = repository.get_incidents(session_id, limit=500)
    summary = repository.session_summary(session_id)
    return ReportContext(
        session=session,
        stats=stats,
        incidents=incidents,
        health_score=summary.health_score if summary else 0,
        health_status=summary.status.label if summary else "",
        uptime_s=session.duration_s,
    )


def _percentiles(stats: TargetStats) -> list[str]:
    return [
        f"  Average: {format_latency(stats.average_ms)}",
        f"  Median:  {format_latency(stats.median_ms)}",
        f"  P95:     {format_latency(stats.p95_ms)}",
        f"  P99:     {format_latency(stats.p99_ms)}",
        f"  Min:     {format_latency(stats.min_ms)}",
        f"  Max:     {format_latency(stats.max_ms)}",
    ]


def _target_section(stats: TargetStats) -> list[str]:
    lines = [f"{stats.target_name} [{TargetCategory(stats.category).label}]  "
             f"{stats.status.symbol} {stats.status.label}"]
    lines.extend(_percentiles(stats))
    lines.append(f"  Jitter:  {format_latency(stats.jitter_ms)}")
    lines.append(f"  Loss:    {stats.loss_percent:.2f}%  "
                 f"({stats.failed_count} of {stats.sample_count} probes)")
    lines.append(f"  Spikes:  {stats.spike_count}")
    return lines


def _incident_lines(incidents: list[Incident], limit: int = 20) -> list[str]:
    if not incidents:
        return ["No lag incidents were recorded."]
    lines = []
    ordered = sorted(incidents, key=lambda i: i.start)
    for incident in ordered[-limit:]:
        lines.append(
            f"{format_clock(incident.start)} - {format_clock(incident.end)}  "
            f"{incident.severity.label.upper():<8} {incident.target_name}: "
            f"peak {format_latency(incident.peak_latency_ms)} "
            f"(baseline {format_latency(incident.baseline_latency_ms)}, "
            f"{format_duration(incident.duration_s)})"
        )
        if incident.diagnosis:
            lines.append(f"    {incident.confidence.wording}: {incident.diagnosis}")
    if len(ordered) > limit:
        lines.append(f"... and {len(ordered) - limit} earlier incident(s)")
    return lines


def _correlation_evidence(incidents: list[Incident]) -> list[str]:
    """The router-vs-destination comparison that plan section 97 asks for."""
    if not incidents:
        return []
    total = len(incidents)
    gateway_stable = sum(
        1 for i in incidents if i.correlation.get("gateway_stable") is True
    )
    internet_stable = sum(
        1 for i in incidents if i.correlation.get("internet_stable") is True
    )
    gateway_affected = sum(
        1 for i in incidents if i.correlation.get("gateway_stable") is False
    )
    lines = [
        f"Incidents analysed: {total}",
        f"Local gateway remained stable during {gateway_stable} of {total} incidents.",
        f"Public internet targets remained stable during {internet_stable} of {total} "
        f"incidents.",
    ]
    if gateway_affected:
        lines.append(
            f"The local gateway was also affected during {gateway_affected} incident(s)."
        )
    peaks = [i.peak_latency_ms for i in incidents]
    if peaks:
        lines.append(
            f"Largest recorded peak: {format_latency(max(peaks))}; "
            f"median incident peak: {format_latency(statistics.median(peaks))}."
        )
    return lines


def _severity_breakdown(incidents: list[Incident]) -> list[str]:
    if not incidents:
        return []
    counts = {severity: 0 for severity in Severity}
    for incident in incidents:
        counts[incident.severity] += 1
    return [
        "  " + "  ".join(f"{s.label}: {counts[s]}" for s in Severity)
    ]


def build_session_report(context: ReportContext, title: str = "REPORT") -> str:
    """Standard diagnostic report (plan section 42)."""
    lines: list[str] = [DIVIDER, f"{APP_NAME.upper()} {title}", DIVIDER, ""]

    if context.session:
        start = format_datetime(context.session.start_time)
        end = (
            format_datetime(context.session.end_time)
            if context.session.end_time
            else "ongoing"
        )
        lines.append(f"Session:   {context.session.name or context.session.id}")
        lines.append(f"Period:    {start} -> {end}")
    lines.append(f"Duration:  {format_duration(context.uptime_s)}")
    lines.append(f"Generated: {format_datetime(now_ts())}")
    lines.append(f"Platform:  {platform_name()}")
    lines.append("")

    lines.append(f"OVERALL HEALTH: {context.health_score} / 100  ({context.health_status})")
    lines.append("Note: the health score is a diagnostic indicator, not an exact measure.")
    lines.append("")

    lines.append(SUBDIVIDER)
    lines.append("PER-TARGET STATISTICS")
    lines.append(SUBDIVIDER)
    if context.stats:
        for stats in context.stats.values():
            if not stats.sample_count:
                continue
            lines.extend(_target_section(stats))
            lines.append("")
    else:
        lines.append("No measurements were recorded.")
        lines.append("")

    lines.append(SUBDIVIDER)
    lines.append(f"LAG INCIDENTS ({len(context.incidents)})")
    lines.append(SUBDIVIDER)
    lines.extend(_severity_breakdown(context.incidents))
    lines.extend(_incident_lines(context.incidents))
    lines.append("")

    if context.diagnostic:
        lines.append(SUBDIVIDER)
        lines.append("LAYER ANALYSIS")
        lines.append(SUBDIVIDER)
        for layer in context.diagnostic.layers:
            lines.append(f"{layer.name}: {layer.status.symbol} {layer.status.label} "
                         f"- {layer.summary}")
            for line in layer.lines:
                lines.append(f"  {line}")
            lines.append("")

        diagnosis = context.diagnostic.diagnosis
        lines.append(SUBDIVIDER)
        lines.append("FINDING")
        lines.append(SUBDIVIDER)
        lines.append(diagnosis.wording)
        lines.append(f"Confidence: {diagnosis.confidence.label}")
        if diagnosis.detail:
            lines.append("")
            lines.append(diagnosis.detail)
        if diagnosis.evidence:
            lines.append("")
            lines.append("Evidence:")
            for item in diagnosis.evidence:
                lines.append(f"  - {item}")
        lines.append("")

    evidence = _correlation_evidence(context.incidents)
    if evidence:
        lines.append(SUBDIVIDER)
        lines.append("CORRELATION EVIDENCE")
        lines.append(SUBDIVIDER)
        lines.extend(evidence)
        lines.append("")

    for note in context.notes:
        lines.append(note)
    if context.notes:
        lines.append("")

    lines.append(DIVIDER)
    return "\n".join(lines)


def build_isp_report(context: ReportContext) -> str:
    """Evidence-oriented report for a support ticket (plan section 69)."""
    lines: list[str] = [DIVIDER, f"{APP_NAME.upper()} - CONNECTION EVIDENCE REPORT", DIVIDER, ""]
    lines.append("This report was produced by continuous automated measurement from the")
    lines.append("customer's computer. It compares the local router, several independent")
    lines.append("public targets and the customer's destination server, so problems can be")
    lines.append("separated by layer rather than guessed at.")
    lines.append("")

    if context.session:
        lines.append(f"Test period: {format_datetime(context.session.start_time)} -> "
                     f"{format_datetime(context.session.end_time) if context.session.end_time else 'ongoing'}")
    lines.append(f"Duration:    {format_duration(context.uptime_s)}")
    lines.append(f"Generated:   {format_datetime(now_ts())}")
    lines.append("")

    if context.network_info:
        lines.append(SUBDIVIDER)
        lines.append("CONNECTION DETAILS")
        lines.append(SUBDIVIDER)
        for key, value in context.network_info.items():
            lines.append(f"{key}: {value}")
        lines.append("")

    if context.wifi_info:
        lines.append(SUBDIVIDER)
        lines.append("WI-FI")
        lines.append(SUBDIVIDER)
        for key, value in context.wifi_info.items():
            lines.append(f"{key}: {value}")
        lines.append("")

    lines.append(SUBDIVIDER)
    lines.append("MEASUREMENT SUMMARY")
    lines.append(SUBDIVIDER)
    for category in (TargetCategory.GATEWAY, TargetCategory.INTERNET, TargetCategory.CUSTOM):
        group = [s for s in context.stats.values()
                 if s.category == category.value and s.sample_count]
        if not group:
            continue
        lines.append(f"[{category.label}]")
        for stats in group:
            lines.extend(_target_section(stats))
            lines.append("")

    lines.append(SUBDIVIDER)
    lines.append("INCIDENT TIMELINE")
    lines.append(SUBDIVIDER)
    lines.extend(_incident_lines(context.incidents, limit=50))
    lines.append("")

    evidence = _correlation_evidence(context.incidents)
    if evidence:
        lines.append(SUBDIVIDER)
        lines.append("EVIDENCE")
        lines.append(SUBDIVIDER)
        lines.extend(evidence)
        lines.append("")

    if context.traceroutes:
        lines.append(SUBDIVIDER)
        lines.append("TRACEROUTE")
        lines.append(SUBDIVIDER)
        for trace in context.traceroutes[:3]:
            lines.append(f"Target: {trace.get('target')} "
                         f"({format_datetime(trace.get('timestamp', now_ts()))})")
            for hop in trace.get("hops", []):
                best = hop.get("best_ms")
                host = hop.get("host") or hop.get("ip") or "*"
                lines.append(f"  Hop {hop.get('number', '?'):>2}  {host:<40} "
                             f"{format_latency(best)}")
            lines.append("")
        lines.append("Note: routers commonly deprioritise ICMP, so a single slow hop is not")
        lines.append("proof of a fault at that hop.")
        lines.append("")

    if context.diagnostic:
        diagnosis = context.diagnostic.diagnosis
        lines.append(SUBDIVIDER)
        lines.append("CUSTOMER-SIDE FINDING")
        lines.append(SUBDIVIDER)
        lines.append(diagnosis.wording)
        lines.append(f"Confidence: {diagnosis.confidence.label}")
        if diagnosis.confidence == Confidence.UNCLEAR:
            lines.append("The data does not isolate a single cause; the measurements above")
            lines.append("are provided so they can be interpreted alongside network-side data.")
        for item in diagnosis.evidence:
            lines.append(f"  - {item}")
        lines.append("")

    lines.append(DIVIDER)
    return "\n".join(lines)


def build_incident_report(incident: Incident) -> str:
    """Detail text for a single incident (plan sections 11, 90)."""
    lines = [
        f"{incident.severity.label.upper()} INCIDENT",
        "",
        f"Start:     {format_clock(incident.start, with_millis=True)}",
        f"End:       {format_clock(incident.end, with_millis=True)}",
        f"Duration:  {format_duration(incident.duration_s)}",
        "",
        f"Baseline:  {format_latency(incident.baseline_latency_ms)}",
        f"Peak:      {format_latency(incident.peak_latency_ms)}",
        f"Increase:  +{format_latency(incident.deviation_ms)}"
        + (f"  ({incident.ratio:.2f}x)" if incident.ratio else ""),
        "",
        f"Target:    {incident.target_name} "
        f"[{TargetCategory(incident.category).label}]",
        f"Samples:   {incident.sample_count} ({incident.failed_count} failed)",
        f"Loss:      {incident.packet_loss * 100:.1f}%",
        "",
    ]
    entries = incident.correlation.get("entries", [])
    if entries:
        lines.append("Other targets in the correlation window:")
        for entry in entries:
            status = "affected" if (entry.get("spiked") or entry.get("failures")) else "stable"
            lines.append(
                f"  {entry.get('target_name', '?'):<24} "
                f"{format_latency(entry.get('average_ms')):>9}  {status}"
            )
        lines.append("")
    if incident.diagnosis:
        lines.append(f"{incident.confidence.wording}: {incident.diagnosis}")
        lines.append(f"Confidence: {incident.confidence.label}")
    for item in incident.evidence:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def save_report(text: str, path) -> bool:
    from pathlib import Path

    try:
        Path(path).write_text(text, encoding="utf-8")
        log.info("Report written to %s", path)
        return True
    except OSError as exc:
        log.error("Could not write report to %s: %s", path, exc)
        return False


def status_line(stats: TargetStats) -> str:
    """One-line target summary used in compact views."""
    return (
        f"{stats.status.symbol} {stats.target_name}: {format_latency(stats.current_ms)} "
        f"(avg {format_latency(stats.average_ms)}, loss {stats.loss_percent:.1f}%)"
    )


def health_headline(score: int, status: str, node_status: NodeStatus) -> str:
    return f"{node_status.symbol} {score}/100 {status}"
