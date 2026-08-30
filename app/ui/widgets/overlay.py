"""Always-on-top overlay (plan sections 45, 46).

A small frameless window that stays above the game: latency, jitter, loss and a
status dot. It is draggable, its opacity and font size are configurable, and it
never steals focus.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.config.defaults import NodeStatus
from app.config.settings import GamingSettings
from app.ui.theme import PALETTE, RADIUS_SMALL, scaled_font
from app.utils.time import format_latency

METRIC_LABELS = {
    "latency": "",
    "jitter": "J",
    "loss": "L",
    "spikes": "S",
    "status": "",
}


class OverlayWindow(QWidget):
    """Frameless, always-on-top metrics strip."""

    def __init__(self, settings: GamingSettings, parent: QWidget | None = None) -> None:
        super().__init__(None)
        self.settings = settings
        self._drag_offset: QPoint | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(settings.overlay_opacity)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(12)

        self.dot = QLabel("●")
        self.latency = QLabel("--")
        self.jitter = QLabel("J --")
        self.loss = QLabel("L --")
        self.spikes = QLabel("S 0")

        for widget in (self.dot, self.latency, self.jitter, self.loss, self.spikes):
            widget.setFont(scaled_font(settings.overlay_font_size * 0.75))
            layout.addWidget(widget)

        self.latency.setFont(
            scaled_font(settings.overlay_font_size * 0.95, weight=700)
        )
        self.setToolTip("Internet Overwatch overlay - drag to move")
        self.apply_settings(settings)
        self.move(settings.overlay_x, settings.overlay_y)

    def apply_settings(self, settings: GamingSettings) -> None:
        self.settings = settings
        self.setWindowOpacity(settings.overlay_opacity)
        visible = set(settings.overlay_metrics)
        self.jitter.setVisible("jitter" in visible)
        self.loss.setVisible("loss" in visible)
        self.spikes.setVisible("spikes" in visible)
        self.dot.setVisible("status" in visible)
        for widget in (self.jitter, self.loss, self.spikes, self.dot):
            widget.setFont(scaled_font(settings.overlay_font_size * 0.75))
        self.latency.setFont(scaled_font(settings.overlay_font_size * 0.95, weight=700))
        self.adjustSize()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QColor(PALETTE.background)
        background.setAlpha(225)
        painter.setBrush(background)
        painter.setPen(QColor(PALETTE.border_strong))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), RADIUS_SMALL, RADIUS_SMALL)
        painter.end()

    def update_metrics(self, stats, spike_count: int) -> None:
        if stats is None:
            self.latency.setText("idle")
            self.dot.setStyleSheet(f"color: {PALETTE.unknown};")
            return
        status: NodeStatus = stats.status
        color = PALETTE.status_color(status)
        self.dot.setStyleSheet(f"color: {color};")
        self.dot.setToolTip(status.label)
        self.latency.setText(format_latency(stats.current_ms))
        self.latency.setStyleSheet(f"color: {color};")
        self.jitter.setText(f"J {format_latency(stats.jitter_ms)}")
        self.loss.setText(f"L {stats.loss_percent:.1f}%")
        self.spikes.setText(f"S {spike_count}")
        self.adjustSize()

    # --------------------------------------------------------------- drag ---
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self._drag_offset = None
        self.settings.overlay_x = self.x()
        self.settings.overlay_y = self.y()
        event.accept()
