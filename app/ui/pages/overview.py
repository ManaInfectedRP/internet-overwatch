"""Overview page - the dashboard the app opens into (plan sections 6, 8-13, 99).

Reading order follows plan section 54: health, metrics, live graph, connection
path, events. Everything on this page is fed from the monitor snapshot on each
UI tick; nothing here performs measurements.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import Confidence, NodeStatus, TargetCategory
from app.core.detector import classify_jitter, classify_loss
from app.services.monitoring_service import MonitoringService
from app.storage.models import Event, Incident
from app.ui.theme import PALETTE, SPACING
from app.ui.widgets.event_log import EventLogCard
from app.ui.widgets.latency_card import MetricRow, TargetListCard
from app.ui.widgets.live_graph import LiveGraph
from app.ui.widgets.network_map import ConnectionPath, build_path
from app.ui.widgets.status_card import Card, HealthCard
from app.utils.time import format_bps, format_latency

COMPACT_WIDTH = 1100


class DiagnosisCard(Card):
    """Current finding with its confidence, always worded honestly."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Likely cause", parent)
        self.headline = QLabel("Waiting for measurements...")
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet(f"color: {PALETTE.text}; font-size: 12pt; font-weight: 600;")
        self.add(self.headline)

        self.confidence = QLabel("")
        self.confidence.setObjectName("Muted")
        self.add(self.confidence)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("Faint")
        self.add(self.detail)
        self.body().addStretch(1)

    def update_diagnosis(self, diagnosis) -> None:
        self.headline.setText(diagnosis.headline)
        color = {
            Confidence.LIKELY: PALETTE.warning,
            Confidence.POSSIBLE: PALETTE.text,
            Confidence.UNCLEAR: PALETTE.text_muted,
        }[diagnosis.confidence]
        if diagnosis.layer == "none":
            color = PALETTE.healthy
        self.headline.setStyleSheet(f"color: {color}; font-size: 12pt; font-weight: 600;")
        self.confidence.setText(
            f"{diagnosis.confidence.wording} · Confidence: {diagnosis.confidence.label}"
        )
        self.detail.setText(diagnosis.detail)


class ThroughputCard(Card):
    """Passive throughput readout (plan section 31)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Network activity", parent)
        row = QHBoxLayout()
        row.setSpacing(20)

        self.download = QLabel("--")
        self.download.setObjectName("MetricValue")
        self.upload = QLabel("--")
        self.upload.setObjectName("MetricValue")

        for label, widget in (("Download", self.download), ("Upload", self.upload)):
            block = QVBoxLayout()
            block.setSpacing(1)
            name = QLabel(label.upper())
            name.setObjectName("MetricLabel")
            block.addWidget(widget)
            block.addWidget(name)
            row.addLayout(block)
        row.addStretch(1)
        self.add_layout(row)

        self.note = QLabel("Measured passively from adapter counters - no test traffic.")
        self.note.setObjectName("Faint")
        self.note.setWordWrap(True)
        self.add(self.note)

    def update_sample(self, sample) -> None:
        if sample is None:
            return
        self.download.setText(format_bps(sample.download_bps))
        self.upload.setText(format_bps(sample.upload_bps))
        self.note.setText(
            f"CPU {sample.cpu_percent:.0f}% · Memory {sample.memory_percent:.0f}% · "
            f"passive measurement"
        )


class OverviewPage(QWidget):
    """Composite dashboard page."""

    incident_selected = Signal(object)
    event_selected = Signal(object)

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._compact = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        self.layout_ = QVBoxLayout(content)
        self.layout_.setContentsMargins(SPACING + 4, SPACING + 4, SPACING + 4, SPACING + 4)
        self.layout_.setSpacing(SPACING)

        # Row 1: health + diagnosis
        top_row = QHBoxLayout()
        top_row.setSpacing(SPACING)
        self.health_card = HealthCard()
        self.health_card.setMinimumHeight(172)
        self.diagnosis_card = DiagnosisCard()
        top_row.addWidget(self.health_card, 3)
        top_row.addWidget(self.diagnosis_card, 2)
        self.layout_.addLayout(top_row)

        # Row 2: metric cards
        self.metrics = MetricRow()
        self.layout_.addWidget(self.metrics)

        # Row 3: live graph
        graph_card = Card("Latency timeline")
        self.graph = LiveGraph()
        self.graph.setMinimumHeight(230)
        # Capped so the connection path and event feed stay above the fold on a
        # 900px window (plan section 54 - state understood in 2-3 seconds).
        self.graph.setMaximumHeight(280)
        graph_card.add(self.graph, 1)
        self.measurement_note = QLabel("")
        self.measurement_note.setObjectName("Faint")
        graph_card.add(self.measurement_note)
        self.layout_.addWidget(graph_card, 1)
        self.graph_card = graph_card

        # Row 4: connection path
        path_card = Card("Connection path")
        self.path = ConnectionPath()
        path_card.add(self.path)
        self.path_note = QLabel(
            "Each node shows the measured latency to that point, not the latency of "
            "the link alone."
        )
        self.path_note.setObjectName("Faint")
        self.path_note.setWordWrap(True)
        path_card.add(self.path_note)
        self.layout_.addWidget(path_card)

        # Row 5: targets + throughput + events
        bottom = QHBoxLayout()
        bottom.setSpacing(SPACING)

        left = QVBoxLayout()
        left.setSpacing(SPACING)
        self.targets_card = TargetListCard("Monitored targets")
        self.throughput_card = ThroughputCard()
        left.addWidget(self.targets_card)
        left.addWidget(self.throughput_card)
        left.addStretch(1)

        self.events_card = EventLogCard()
        self.events_card.setMinimumHeight(230)
        self.events_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.events_card.event_activated.connect(self.event_selected)

        bottom.addLayout(left, 2)
        bottom.addWidget(self.events_card, 3)
        self.layout_.addLayout(bottom)
        self.bottom_layout = bottom

        self.graph.spike_clicked.connect(self._on_spike_clicked)

    # ------------------------------------------------------------- updates ---
    def refresh_targets(self) -> None:
        targets = self.service.targets()
        self.graph.set_targets(targets)
        notes = [
            f"{t.name}: {t.measurement_label}"
            for t in targets
            if t.protocol != "icmp"
        ]
        self.measurement_note.setText(" · ".join(notes))
        self.measurement_note.setVisible(bool(notes))

    def refresh(self) -> None:
        """Redraw everything from the current monitor snapshot."""
        service = self.service
        stats = service.stats()
        settings = service.settings

        if not service.running and not stats:
            self.health_card.clear()
            self.metrics.update_metrics(None, 0, 0.0)
            self.path.set_nodes([])
            return

        health = service.health()
        self.health_card.update_health(health)
        self.health_card.set_connectivity_note(service.connectivity_state())

        primary_id = service.primary_target_id()
        primary = stats.get(primary_id)
        loss_status = jitter_status = None
        if primary is not None:
            _, loss_status = classify_loss(primary.loss_fraction, settings.detection)
            _, jitter_status = classify_jitter(primary.jitter_ms)
        self.metrics.update_metrics(
            primary, service.spike_count(), service.uptime_s(), loss_status, jitter_status
        )

        self.graph.update_from_buffers(
            {tid: service.buffer(tid) for tid in stats}, primary_id
        )
        self.path.set_nodes(build_path(stats))

        colors = {t.id: t.color for t in service.targets()}
        notes = {t.id: t.measurement_label for t in service.targets()}
        self.targets_card.sync(stats, colors, notes)

        self.throughput_card.update_sample(service.last_system_sample())
        self.diagnosis_card.update_diagnosis(service.report().diagnosis)

    def add_event(self, event: Event) -> None:
        self.events_card.add_event(
            event, self.service.settings.appearance.show_millis_in_events
        )

    def set_events(self, events: list[Event]) -> None:
        self.events_card.set_events(
            events, self.service.settings.appearance.show_millis_in_events
        )

    def clear(self) -> None:
        self.health_card.clear()
        self.metrics.update_metrics(None, 0, 0.0)
        self.graph.clear()
        self.path.set_nodes([])
        self.targets_card.clear()
        self.events_card.clear()

    # ------------------------------------------------------------ layout ----
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().resizeEvent(event)
        compact = self.width() < COMPACT_WIDTH
        if compact != self._compact:
            self._compact = compact
            self.metrics.set_compact(compact)
            self.graph.setMinimumHeight(180 if compact else 230)
            self.bottom_layout.setDirection(
                QHBoxLayout.Direction.TopToBottom if compact
                else QHBoxLayout.Direction.LeftToRight
            )

    def _on_spike_clicked(self, timestamp: float, latency: float) -> None:
        """Open the incident that contains the clicked spike marker."""
        for incident in self.service.recent_incidents(100):
            if incident.start - 1.0 <= timestamp <= incident.end + 1.0:
                self.incident_selected.emit(incident)
                return
        # No merged incident yet (it may still be open): show what we know.
        placeholder = Incident(
            start=timestamp,
            end=timestamp,
            peak_latency_ms=latency,
            target_name="spike",
        )
        self.incident_selected.emit(placeholder)
