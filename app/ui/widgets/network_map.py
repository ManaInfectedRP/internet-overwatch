"""Connection path visualisation (plan sections 12, 75).

Draws PC -> Router -> Internet -> Destination as a chain of nodes with the
measured latency on each link. Each node shows latency, loss and a status that
is spelled out in words as well as coloured.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.config.defaults import NodeStatus, TargetCategory
from app.storage.models import TargetStats
from app.ui.theme import PALETTE, RADIUS, scaled_font
from app.utils.time import format_latency


@dataclass
class PathNode:
    label: str
    sublabel: str = ""
    latency_ms: float | None = None
    loss_fraction: float | None = None
    status: NodeStatus = NodeStatus.UNKNOWN
    target_id: int | None = None
    is_local: bool = False

    @property
    def latency_text(self) -> str:
        return format_latency(self.latency_ms)

    @property
    def loss_text(self) -> str:
        if self.loss_fraction is None:
            return ""
        return f"{self.loss_fraction * 100:.1f}% loss"


def build_path(stats: dict[int | None, TargetStats]) -> list[PathNode]:
    """Turn per-target stats into the PC -> ... -> destination chain."""
    nodes = [PathNode(label="PC", sublabel="this computer", status=NodeStatus.HEALTHY,
                      is_local=True)]

    def add_group(category: TargetCategory, fallback_label: str) -> None:
        group = [s for s in stats.values() if s.category == category.value and s.sample_count]
        if not group:
            return
        if category == TargetCategory.INTERNET and len(group) > 1:
            # Several public targets collapse into one "Internet" node showing
            # the best of them; per-target detail stays in the target list.
            best = min(group, key=lambda s: (s.status.rank, s.average_ms or 1e9))
            worst_status = max((s.status for s in group), key=lambda s: s.rank)
            nodes.append(PathNode(
                label="Internet",
                sublabel=f"{len(group)} public targets",
                latency_ms=best.average_ms,
                loss_fraction=max(s.loss_fraction for s in group),
                status=worst_status,
            ))
            return
        for stats_item in group:
            nodes.append(PathNode(
                label=stats_item.target_name or fallback_label,
                sublabel=TargetCategory(stats_item.category).label,
                latency_ms=stats_item.average_ms,
                loss_fraction=stats_item.loss_fraction,
                status=stats_item.status,
                target_id=stats_item.target_id,
            ))

    add_group(TargetCategory.GATEWAY, "Router")
    add_group(TargetCategory.INTERNET, "Internet")
    add_group(TargetCategory.CUSTOM, "Destination")
    return nodes


class ConnectionPath(QWidget):
    """Horizontal node chain; falls back to a vertical stack when narrow."""

    node_clicked = Signal(object)  # PathNode

    NODE_WIDTH = 132
    NODE_HEIGHT = 74
    GAP = 58

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nodes: list[PathNode] = []
        self._rects: list[tuple[QRectF, PathNode]] = []
        self.setMinimumHeight(self.NODE_HEIGHT + 34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMouseTracking(True)

    def set_nodes(self, nodes: list[PathNode]) -> None:
        self._nodes = nodes
        self.updateGeometry()
        self.update()

    def sizeHint(self):  # noqa: N802 - Qt signature
        from PySide6.QtCore import QSize

        count = max(1, len(self._nodes))
        if self._vertical():
            return QSize(260, count * (self.NODE_HEIGHT + 28))
        return QSize(count * (self.NODE_WIDTH + self.GAP), self.NODE_HEIGHT + 34)

    def _vertical(self) -> bool:
        needed = len(self._nodes) * (self.NODE_WIDTH + self.GAP)
        return bool(self._nodes) and needed > self.width()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._rects = []

        if not self._nodes:
            painter.setPen(QColor(PALETTE.text_faint))
            painter.setFont(scaled_font(9.5))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No path data yet - start monitoring")
            painter.end()
            return

        if self._vertical():
            self._paint_vertical(painter)
        else:
            self._paint_horizontal(painter)
        painter.end()

    # ------------------------------------------------------------- layouts ---
    def _paint_horizontal(self, painter: QPainter) -> None:
        count = len(self._nodes)
        total = count * self.NODE_WIDTH + (count - 1) * self.GAP
        x = max(4.0, (self.width() - total) / 2)
        y = (self.height() - self.NODE_HEIGHT) / 2

        for index, node in enumerate(self._nodes):
            rect = QRectF(x, y, self.NODE_WIDTH, self.NODE_HEIGHT)
            self._draw_node(painter, rect, node)
            self._rects.append((rect, node))
            if index < count - 1:
                link_start = QPointF(rect.right(), rect.center().y())
                link_end = QPointF(rect.right() + self.GAP, rect.center().y())
                self._draw_link(painter, link_start, link_end,
                                self._nodes[index + 1], horizontal=True)
            x += self.NODE_WIDTH + self.GAP

    def _paint_vertical(self, painter: QPainter) -> None:
        x = (self.width() - self.NODE_WIDTH) / 2
        y = 6.0
        step = self.NODE_HEIGHT + 28
        for index, node in enumerate(self._nodes):
            rect = QRectF(x, y, self.NODE_WIDTH, self.NODE_HEIGHT)
            self._draw_node(painter, rect, node)
            self._rects.append((rect, node))
            if index < len(self._nodes) - 1:
                self._draw_link(
                    painter,
                    QPointF(rect.center().x(), rect.bottom()),
                    QPointF(rect.center().x(), rect.bottom() + 28),
                    self._nodes[index + 1],
                    horizontal=False,
                )
            y += step

    # --------------------------------------------------------------- parts ---
    def _draw_node(self, painter: QPainter, rect: QRectF, node: PathNode) -> None:
        color = QColor(PALETTE.status_color(node.status))
        painter.setPen(QPen(QColor(PALETTE.border_strong), 1))
        painter.setBrush(QBrush(QColor(PALETTE.surface_alt)))
        painter.drawRoundedRect(rect, RADIUS, RADIUS)

        # Status stripe along the top edge.
        stripe = QRectF(rect.left() + 1, rect.top() + 1, rect.width() - 2, 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(stripe, 2, 2)

        painter.setPen(QColor(PALETTE.text))
        painter.setFont(scaled_font(9.5, QFont.Weight.DemiBold))
        label_rect = QRectF(rect.left() + 6, rect.top() + 8, rect.width() - 12, 18)
        # Target names are user-supplied and can be long; elide rather than clip.
        label = painter.fontMetrics().elidedText(
            node.label, Qt.TextElideMode.ElideRight, int(label_rect.width())
        )
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(color)
        painter.setFont(scaled_font(11, QFont.Weight.Bold))
        latency = node.latency_text if not node.is_local else "local"
        painter.drawText(
            QRectF(rect.left() + 6, rect.top() + 26, rect.width() - 12, 20),
            Qt.AlignmentFlag.AlignCenter,
            latency,
        )

        painter.setPen(QColor(PALETTE.text_faint))
        painter.setFont(scaled_font(7.5))
        detail = node.loss_text or node.sublabel
        painter.drawText(
            QRectF(rect.left() + 6, rect.top() + 45, rect.width() - 12, 14),
            Qt.AlignmentFlag.AlignCenter,
            detail,
        )

        painter.setPen(color)
        painter.setFont(scaled_font(7.5, QFont.Weight.DemiBold))
        painter.drawText(
            QRectF(rect.left() + 6, rect.bottom() - 19, rect.width() - 12, 14),
            Qt.AlignmentFlag.AlignCenter,
            f"{node.status.symbol} {node.status.label}",
        )

    def _draw_link(self, painter: QPainter, start: QPointF, end: QPointF,
                   next_node: PathNode, horizontal: bool) -> None:
        color = QColor(PALETTE.status_color(next_node.status))
        color.setAlpha(190)
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(start, end)

        # Arrow head.
        path = QPainterPath()
        if horizontal:
            path.moveTo(end)
            path.lineTo(end.x() - 7, end.y() - 4)
            path.lineTo(end.x() - 7, end.y() + 4)
        else:
            path.moveTo(end)
            path.lineTo(end.x() - 4, end.y() - 7)
            path.lineTo(end.x() + 4, end.y() - 7)
        path.closeSubpath()
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)

        painter.setPen(QColor(PALETTE.text_muted))
        painter.setFont(scaled_font(7.5))
        label = next_node.latency_text
        if horizontal:
            box = QRectF(start.x(), start.y() - 22, end.x() - start.x(), 16)
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        else:
            box = QRectF(start.x() + 8, start.y() + 4, 80, 16)
            painter.drawText(box, Qt.AlignmentFlag.AlignLeft, label)

    # --------------------------------------------------------- interaction ---
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt signature
        position = event.position()
        for rect, node in self._rects:
            if rect.contains(position):
                self.node_clicked.emit(node)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        position = event.position()
        for rect, node in self._rects:
            if rect.contains(position):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(
                    f"{node.label}\n{node.sublabel}\n"
                    f"Latency: {node.latency_text}\n"
                    f"{node.loss_text or 'Loss: 0.0%'}\n"
                    f"Status: {node.status.label}"
                )
                return
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("")
        super().mouseMoveEvent(event)
