"""Packet loss and jitter visualisation (plan sections 20, 21, 24).

The strip shows the recent probe outcomes one tick per probe, which makes the
difference between "one dropped packet" and "a run of ten" immediately visible
in a way a percentage cannot.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.config.defaults import NodeStatus
from app.config.settings import DetectionSettings
from app.core.detector import classify_jitter, classify_loss
from app.storage.models import TargetStats
from app.ui.theme import PALETTE
from app.ui.widgets.status_card import Card
from app.utils.time import format_latency


class LossStrip(QWidget):
    """Sparkline of probe outcomes: filled = success, red tick = failure."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._outcomes: list[bool] = []
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_outcomes(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(PALETTE.surface_alt))

        if not self._outcomes:
            painter.setPen(QColor(PALETTE.text_faint))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no probes yet")
            painter.end()
            return

        count = len(self._outcomes)
        width = self.width() / count
        painter.setPen(Qt.PenStyle.NoPen)
        for index, ok in enumerate(self._outcomes):
            color = QColor(PALETTE.healthy if ok else PALETTE.problem)
            if ok:
                color.setAlpha(150)
            rect = QRectF(index * width, 4 if ok else 0,
                          max(1.0, width - 0.5), self.height() - (8 if ok else 0))
            painter.setBrush(QBrush(color))
            painter.drawRect(rect)
        painter.end()


class PacketLossCard(Card):
    """Loss and jitter for the primary target, with quality bands."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Packet loss & jitter", parent)
        self.settings = DetectionSettings()

        numbers = QHBoxLayout()
        numbers.setSpacing(24)

        self.loss_block, self.loss_value, self.loss_band = self._block("Loss")
        self.jitter_block, self.jitter_value, self.jitter_band = self._block("Jitter")
        numbers.addWidget(self.loss_block)
        numbers.addWidget(self.jitter_block)
        numbers.addStretch(1)
        self.add_layout(numbers)

        self.strip = LossStrip()
        self.add(self.strip)

        self.caption = QLabel("Each tick is one probe; red marks a lost packet.")
        self.caption.setObjectName("Faint")
        self.add(self.caption)

    @staticmethod
    def _block(label: str) -> tuple[QWidget, QLabel, QLabel]:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        value = QLabel("--")
        value.setObjectName("MetricValue")
        name = QLabel(label.upper())
        name.setObjectName("MetricLabel")
        band = QLabel("")
        band.setObjectName("MetricSub")

        layout.addWidget(value)
        layout.addWidget(name)
        layout.addWidget(band)
        return container, value, band

    def update_stats(self, stats: TargetStats | None, outcomes: list[bool] | None = None,
                     settings: DetectionSettings | None = None) -> None:
        self.settings = settings or self.settings
        if stats is None:
            self.loss_value.setText("--")
            self.jitter_value.setText("--")
            self.loss_band.setText("")
            self.jitter_band.setText("")
            self.strip.set_outcomes([])
            return

        loss_label, loss_status = classify_loss(stats.loss_fraction, self.settings)
        self.loss_value.setText(f"{stats.loss_percent:.1f} %")
        self.loss_value.setStyleSheet(
            f"#MetricValue {{ color: {PALETTE.status_color(loss_status)}; "
            f"font-weight: 600; }}"
        )
        self.loss_band.setText(
            f"{loss_label} · {stats.failed_count}/{stats.sample_count} probes"
        )

        jitter_label, jitter_status = classify_jitter(stats.jitter_ms)
        self.jitter_value.setText(format_latency(stats.jitter_ms))
        self.jitter_value.setStyleSheet(
            f"#MetricValue {{ color: {PALETTE.status_color(jitter_status)}; "
            f"font-weight: 600; }}"
        )
        self.jitter_band.setText(jitter_label)

        if outcomes is not None:
            self.strip.set_outcomes(outcomes)

    def status(self) -> NodeStatus:
        return NodeStatus.UNKNOWN
