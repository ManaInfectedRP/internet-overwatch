"""Live Monitor page - the at-a-glance view for while you play (plan section 14).

Deliberately sparse: one enormous latency number, three secondary metrics, the
per-target list and a status line. Nothing here needs interpretation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import NodeStatus
from app.core.detector import classify_jitter, classify_loss
from app.services.monitoring_service import MonitoringService
from app.ui.theme import PALETTE, SPACING, scaled_font
from app.ui.widgets.latency_card import TargetListCard
from app.ui.widgets.live_graph import LiveGraph
from app.ui.widgets.packet_loss import LossStrip
from app.utils.time import format_latency


class BigMetric(QWidget):
    """Large value with a small caption underneath."""

    def __init__(self, caption: str, size: float = 20, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.value = QLabel("--")
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setFont(scaled_font(size, weight=700))
        self.value.setStyleSheet(f"color: {PALETTE.text};")

        self.caption = QLabel(caption.upper())
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setObjectName("MetricLabel")

        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set_value(self, text: str, status: NodeStatus | None = None) -> None:
        self.value.setText(text)
        color = PALETTE.text if status is None else PALETTE.status_color(status)
        self.value.setStyleSheet(f"color: {color};")


class LiveMonitorPage(QWidget):
    """Minimal gaming-oriented view."""

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING + 8, SPACING + 8, SPACING + 8, SPACING + 8)
        layout.setSpacing(SPACING)

        header = QHBoxLayout()
        title = QLabel("LIVE MONITOR")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.target_label = QLabel("")
        self.target_label.setObjectName("Muted")
        header.addWidget(self.target_label)
        layout.addLayout(header)

        self.latency = BigMetric("Latency", size=52)
        self.latency.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.latency)

        secondary = QHBoxLayout()
        secondary.setSpacing(SPACING * 2)
        self.jitter = BigMetric("Jitter", size=20)
        self.loss = BigMetric("Loss", size=20)
        self.spikes = BigMetric("Spikes", size=20)
        for widget in (self.jitter, self.loss, self.spikes):
            secondary.addWidget(widget, 1)
        layout.addLayout(secondary)

        self.strip = LossStrip()
        self.strip.setFixedHeight(20)
        layout.addWidget(self.strip)

        self.graph = LiveGraph()
        self.graph.set_range(60)
        self.graph.setMinimumHeight(170)
        layout.addWidget(self.graph, 1)

        self.targets_card = TargetListCard("Targets")
        layout.addWidget(self.targets_card)

        status_row = QHBoxLayout()
        self.status_label = QLabel("STATUS: NOT MONITORING")
        self.status_label.setStyleSheet(
            f"color: {PALETTE.text_muted}; font-size: 12pt; font-weight: 700;"
        )
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)

        self.overlay_button = QPushButton("Show overlay")
        self.overlay_button.setCheckable(True)
        self.overlay_button.setToolTip(
            "Small always-on-top window with the key metrics"
        )
        status_row.addWidget(self.overlay_button)

        self.gaming_button = QPushButton("Gaming mode")
        self.gaming_button.setCheckable(True)
        self.gaming_button.setToolTip(
            "Reduce UI updates and heavy diagnostics while keeping monitoring active"
        )
        status_row.addWidget(self.gaming_button)
        layout.addLayout(status_row)

    def refresh_targets(self) -> None:
        self.graph.set_targets(self.service.targets())

    def refresh(self) -> None:
        service = self.service
        stats = service.stats()
        primary_id = service.primary_target_id()
        primary = stats.get(primary_id)

        if primary is None:
            self.latency.set_value("--")
            self.jitter.set_value("--")
            self.loss.set_value("--")
            self.spikes.set_value("0")
            self.status_label.setText("STATUS: NOT MONITORING")
            self.status_label.setStyleSheet(
                f"color: {PALETTE.text_muted}; font-size: 12pt; font-weight: 700;"
            )
            return

        self.target_label.setText(f"{primary.target_name} · {primary.sample_count} probes")
        self.latency.set_value(format_latency(primary.current_ms), primary.status)

        _, jitter_status = classify_jitter(primary.jitter_ms)
        _, loss_status = classify_loss(primary.loss_fraction, service.settings.detection)
        self.jitter.set_value(format_latency(primary.jitter_ms), jitter_status)
        self.loss.set_value(f"{primary.loss_percent:.1f}%", loss_status)
        self.spikes.set_value(
            str(service.spike_count()),
            NodeStatus.WARNING if service.spike_count() else NodeStatus.HEALTHY,
        )

        buffer = service.buffer(primary_id)
        if buffer is not None:
            self.strip.set_outcomes(list(buffer.successes)[-160:])

        self.graph.update_from_buffers({tid: service.buffer(tid) for tid in stats}, primary_id)

        colors = {t.id: t.color for t in service.targets()}
        notes = {t.id: t.measurement_label for t in service.targets()}
        self.targets_card.sync(stats, colors, notes)

        connectivity = service.connectivity_state()
        overall = max((s.status for s in stats.values() if s.sample_count),
                      key=lambda s: s.rank, default=NodeStatus.UNKNOWN)
        text = connectivity or f"{overall.symbol} {overall.label}"
        self.status_label.setText(f"STATUS: {text}")
        self.status_label.setStyleSheet(
            f"color: {PALETTE.status_color(overall)}; font-size: 12pt; font-weight: 700;"
        )

    def compact_line(self) -> str:
        """One-line summary used by the overlay (plan section 14)."""
        stats = self.service.stats()
        primary = stats.get(self.service.primary_target_id())
        if primary is None:
            return "OW  idle"
        status = primary.status
        return (
            f"OW  {status.symbol} {format_latency(primary.current_ms)}  "
            f"J:{format_latency(primary.jitter_ms)}  "
            f"L:{primary.loss_percent:.1f}%"
        )

    def clear(self) -> None:
        self.latency.set_value("--")
        self.jitter.set_value("--")
        self.loss.set_value("--")
        self.spikes.set_value("0")
        self.strip.set_outcomes([])
        self.graph.clear()
        self.targets_card.clear()
