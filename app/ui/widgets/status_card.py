"""Card container, health score card and status chip (plan sections 8, 54)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import NodeStatus
from app.storage.models import HealthResult
from app.ui.theme import CARD_PADDING, PALETTE, SPACING, scaled_font


class Card(QFrame):
    """Rounded surface with an optional uppercase title."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        self._layout.setSpacing(SPACING - 4)

        self.title_label: QLabel | None = None
        if title:
            self.title_label = QLabel(title.upper())
            self.title_label.setObjectName("CardTitle")
            self._layout.addWidget(self.title_label)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.addWidget(widget, stretch)

    def add_layout(self, layout, stretch: int = 0) -> None:
        self._layout.addLayout(layout, stretch)

    def set_title(self, title: str) -> None:
        if self.title_label is not None:
            self.title_label.setText(title.upper())


class StatusChip(QLabel):
    """Small pill showing a status as symbol + word (never colour alone)."""

    def __init__(self, status: NodeStatus = NodeStatus.UNKNOWN,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status)

    def set_status(self, status: NodeStatus, text: str | None = None) -> None:
        self._status = status
        label = text if text is not None else status.label
        self.setText(f"{status.symbol}  {label}")
        color = PALETTE.status_color(status)
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 6px; "
            f"padding: 3px 10px; font-weight: 600;"
        )
        self.setToolTip(f"Status: {label}")

    @property
    def status(self) -> NodeStatus:
        return self._status


class HealthGauge(QWidget):
    """Circular gauge with the score drawn in its centre."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._score: int | None = None
        self._status = NodeStatus.UNKNOWN
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_value(self, score: int | None, status: NodeStatus) -> None:
        self._score = None if score is None else max(0, min(100, int(score)))
        self._status = status
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = max(40, min(self.width(), self.height()) - 18)
        left = (self.width() - side) // 2
        top = (self.height() - side) // 2
        rect = self.rect().adjusted(left, top, -(self.width() - side - left),
                                    -(self.height() - side - top))

        thickness = max(6, side // 16)
        track = QPen(QColor(PALETTE.border), thickness)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, 225 * 16, -270 * 16)

        if self._score is not None:
            arc = QPen(QColor(PALETTE.status_color(self._status)), thickness)
            arc.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc)
            painter.drawArc(rect, 225 * 16, int(-270 * 16 * (self._score / 100.0)))

        # Score and the "/ 100" caption occupy separate bands so they never
        # collide as the widget is resized.
        painter.setFont(scaled_font(side / 4.6, QFont.Weight.Bold))
        painter.setPen(QColor(PALETTE.text))
        painter.drawText(
            QRectF(rect.left(), rect.top() + side * 0.24, side, side * 0.34),
            Qt.AlignmentFlag.AlignCenter,
            "--" if self._score is None else str(self._score),
        )

        painter.setFont(scaled_font(max(7.0, side / 17)))
        painter.setPen(QColor(PALETTE.text_faint))
        painter.drawText(
            QRectF(rect.left(), rect.top() + side * 0.59, side, side * 0.14),
            Qt.AlignmentFlag.AlignCenter,
            "/ 100",
        )
        painter.end()


class HealthCard(Card):
    """The dashboard's headline: score, status and the reasons behind it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Internet health", parent)

        row = QHBoxLayout()
        row.setSpacing(SPACING)

        self.gauge = HealthGauge()
        row.addWidget(self.gauge, 1)

        details = QVBoxLayout()
        details.setSpacing(6)
        self.status_chip = StatusChip()
        self.status_chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        details.addWidget(self.status_chip, 0, Qt.AlignmentFlag.AlignLeft)

        self.reason_label = QLabel("Waiting for measurements...")
        self.reason_label.setWordWrap(True)
        self.reason_label.setObjectName("Muted")
        details.addWidget(self.reason_label)

        self.note_label = QLabel(
            "A diagnostic indicator, not an exact measurement."
        )
        self.note_label.setWordWrap(True)
        self.note_label.setObjectName("Faint")
        details.addWidget(self.note_label)
        details.addStretch(1)

        row.addLayout(details, 2)
        self.add_layout(row, 1)

    def update_health(self, health: HealthResult) -> None:
        self.gauge.set_value(health.score, health.node_status)
        self.status_chip.set_status(health.node_status, health.status.label)
        self.reason_label.setText(" · ".join(health.reasons[:3]))

    def clear(self) -> None:
        self.gauge.set_value(None, NodeStatus.UNKNOWN)
        self.status_chip.set_status(NodeStatus.UNKNOWN, "NOT MONITORING")
        self.reason_label.setText("Start monitoring to see your connection health.")

    def set_connectivity_note(self, text: str | None) -> None:
        if text:
            self.note_label.setText(text)
            self.note_label.setStyleSheet(f"color: {PALETTE.problem}; font-weight: 600;")
        else:
            self.note_label.setText("A diagnostic indicator, not an exact measurement.")
            self.note_label.setStyleSheet("")
