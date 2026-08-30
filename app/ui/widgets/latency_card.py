"""Metric cards: latency, jitter, loss, spikes, uptime (plan section 9)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.config.defaults import NodeStatus
from app.storage.models import TargetStats
from app.ui.theme import PALETTE, SPACING
from app.ui.widgets.status_card import Card
from app.utils.time import format_duration, format_latency


class MetricCard(Card):
    """A single big number with a label and an optional sub-line."""

    def __init__(self, label: str, unit: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.body().setSpacing(2)

        self.value_label = QLabel("--")
        self.value_label.setObjectName("MetricValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.name_label = QLabel(label.upper())
        self.name_label.setObjectName("MetricLabel")

        self.sub_label = QLabel("")
        self.sub_label.setObjectName("MetricSub")
        self.sub_label.setWordWrap(True)

        self.add(self.value_label)
        self.add(self.name_label)
        self.add(self.sub_label)
        self.body().addStretch(1)
        self.unit = unit
        self.setMinimumWidth(120)

    def set_value(self, text: str, status: NodeStatus | None = None,
                  sub: str = "") -> None:
        self.value_label.setText(text)
        self.sub_label.setText(sub)
        color = PALETTE.text if status is None else PALETTE.status_color(status)
        # The app stylesheet colours #MetricValue with an ID selector, so this
        # override has to match that specificity to win the cascade.
        self.value_label.setStyleSheet(
            f"#MetricValue {{ color: {color}; font-weight: 600; }}"
        )
        if status is not None:
            # Colour alone never carries the meaning (plan section 91).
            self.setToolTip(f"{self.name_label.text().title()}: {text} - {status.label}")

    def clear(self) -> None:
        self.set_value("--")
        self.sub_label.setText("")


class MetricRow(QWidget):
    """The row of headline metrics on the Overview page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING)

        self.latency = MetricCard("Latency")
        self.jitter = MetricCard("Jitter")
        self.loss = MetricCard("Packet loss")
        self.spikes = MetricCard("Lag spikes")
        self.uptime = MetricCard("Uptime")

        for column, card in enumerate(
            (self.latency, self.jitter, self.loss, self.spikes, self.uptime)
        ):
            layout.addWidget(card, 0, column)
            layout.setColumnStretch(column, 1)
        self._layout = layout

    def set_compact(self, compact: bool) -> None:
        """Stack into two rows on narrow windows (plan section 55)."""
        cards = [self.latency, self.jitter, self.loss, self.spikes, self.uptime]
        for card in cards:
            self._layout.removeWidget(card)
        if compact:
            for index, card in enumerate(cards):
                self._layout.addWidget(card, index // 3, index % 3)
        else:
            for index, card in enumerate(cards):
                self._layout.addWidget(card, 0, index)
        for column in range(3):
            self._layout.setColumnStretch(column, 1)

    def update_metrics(
        self,
        stats: TargetStats | None,
        spike_count: int,
        uptime_s: float,
        loss_status: NodeStatus | None = None,
        jitter_status: NodeStatus | None = None,
    ) -> None:
        if stats is None:
            for card in (self.latency, self.jitter, self.loss, self.spikes):
                card.clear()
            self.uptime.set_value(format_duration(uptime_s))
            return

        latency_status = stats.status if stats.current_ms is not None else NodeStatus.PROBLEM
        self.latency.set_value(
            format_latency(stats.current_ms),
            latency_status,
            sub=(
                f"avg {format_latency(stats.average_ms)} · "
                f"min {format_latency(stats.min_ms)} · "
                f"max {format_latency(stats.max_ms)}"
            ),
        )
        self.jitter.set_value(
            format_latency(stats.jitter_ms),
            jitter_status,
            sub=f"p95 {format_latency(stats.p95_ms)}",
        )
        self.loss.set_value(
            f"{stats.loss_percent:.1f} %",
            loss_status,
            sub=f"{stats.failed_count} of {stats.sample_count} probes",
        )
        self.spikes.set_value(
            str(spike_count),
            NodeStatus.WARNING if spike_count else NodeStatus.HEALTHY,
            sub="this session",
        )
        self.uptime.set_value(format_duration(uptime_s), sub="monitoring")


class TargetRow(QWidget):
    """One line per target: status dot, name, latency, loss."""

    def __init__(self, name: str, category_color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(10)

        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {category_color}; font-size: 13pt;")
        self.dot.setFixedWidth(14)

        self.name_label = QLabel(name)
        self.name_label.setMinimumWidth(120)

        self.latency_label = QLabel("--")
        self.latency_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.latency_label.setMinimumWidth(70)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("Faint")
        self.detail_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        layout.addWidget(self.dot)
        layout.addWidget(self.name_label, 1)
        layout.addWidget(self.latency_label)
        layout.addWidget(self.detail_label)

    def update_stats(self, stats: TargetStats, measurement_note: str = "") -> None:
        color = PALETTE.status_color(stats.status)
        self.dot.setStyleSheet(f"color: {color}; font-size: 13pt;")
        self.dot.setToolTip(stats.status.label)
        if stats.reachable:
            self.latency_label.setText(format_latency(stats.current_ms))
        else:
            self.latency_label.setText("timeout")
        self.latency_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        detail = f"{stats.status.label.title()}"
        if stats.loss_fraction > 0:
            detail += f" · {stats.loss_percent:.1f}% loss"
        self.detail_label.setText(detail)
        tooltip = (
            f"{stats.target_name}\n"
            f"Average {format_latency(stats.average_ms)}, "
            f"p95 {format_latency(stats.p95_ms)}, "
            f"jitter {format_latency(stats.jitter_ms)}\n"
            f"Loss {stats.loss_percent:.2f}% over {stats.sample_count} probes\n"
            f"Spikes: {stats.spike_count}"
        )
        if measurement_note:
            tooltip += f"\nMeasurement: {measurement_note}"
        self.setToolTip(tooltip)


class TargetListCard(Card):
    """Live per-target list used on Overview and Live Monitor."""

    def __init__(self, title: str = "Targets", parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.rows: dict[int | None, TargetRow] = {}
        self._container = QVBoxLayout()
        self._container.setSpacing(0)
        self.add_layout(self._container)
        self.body().addStretch(1)
        self.empty_label = QLabel("No targets are being monitored.")
        self.empty_label.setObjectName("Faint")
        self.add(self.empty_label)

    def sync(self, stats: dict[int | None, TargetStats],
             colors: dict[int | None, str], notes: dict[int | None, str] | None = None) -> None:
        notes = notes or {}
        for target_id, target_stats in stats.items():
            row = self.rows.get(target_id)
            if row is None:
                row = TargetRow(target_stats.target_name,
                                colors.get(target_id, PALETTE.unknown))
                self.rows[target_id] = row
                self._container.addWidget(row)
            row.update_stats(target_stats, notes.get(target_id, ""))

        for target_id in list(self.rows):
            if target_id not in stats:
                row = self.rows.pop(target_id)
                self._container.removeWidget(row)
                row.deleteLater()

        self.empty_label.setVisible(not self.rows)

    def clear(self) -> None:
        for row in self.rows.values():
            self._container.removeWidget(row)
            row.deleteLater()
        self.rows.clear()
        self.empty_label.setVisible(True)
