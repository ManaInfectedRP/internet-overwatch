"""Live latency graph (plan sections 10, 11).

PyQtGraph is used because it redraws high-frequency series cheaply. The graph
reads from the monitor's in-memory ring buffers, never from SQLite, so a redraw
costs nothing but a curve update (plan section 50).

Features: per-target series, zoom/pan, pause, reset, hover readout, spike
markers, selectable time range, average and threshold lines.
"""

from __future__ import annotations

import time
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import LIVE_TIME_RANGES, Severity
from app.storage.models import Target
from app.ui.theme import PALETTE
from app.utils.time import format_clock, format_latency

pg.setConfigOptions(antialias=True, background=PALETTE.graph_bg, foreground=PALETTE.text_muted)


class TimeAxis(pg.AxisItem):
    """X axis that renders UNIX timestamps as wall-clock labels."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph API
        strings = []
        for value in values:
            try:
                dt = datetime.fromtimestamp(value)
            except (ValueError, OSError, OverflowError):
                strings.append("")
                continue
            strings.append(dt.strftime("%H:%M:%S" if spacing < 60 else "%H:%M"))
        return strings


class LiveGraph(QWidget):
    """Multi-series latency plot with its own control strip."""

    spike_clicked = Signal(float, float)  # timestamp, latency

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paused = False
        self._range_seconds = 300
        self._curves: dict[int | None, pg.PlotDataItem] = {}
        self._spike_items: dict[int | None, pg.ScatterPlotItem] = {}
        self._loss_items: dict[int | None, pg.ScatterPlotItem] = {}
        self._visible: dict[int | None, bool] = {}
        self._toggles: dict[int | None, QCheckBox] = {}
        self._targets: dict[int | None, Target] = {}
        self._show_average = True
        self._show_threshold = False
        self._threshold_ms = 100.0
        self._auto_range = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addLayout(self._build_controls())

        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.plot.setLabel("left", "Latency", units="ms")
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setMenuEnabled(False)
        self.plot.setClipToView(True)
        self.plot.getAxis("left").setPen(pg.mkPen(PALETTE.grid))
        self.plot.getAxis("bottom").setPen(pg.mkPen(PALETTE.grid))
        self.plot.setMinimumHeight(200)
        layout.addWidget(self.plot, 1)

        self.average_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(PALETTE.text_faint, width=1, style=Qt.PenStyle.DashLine)
        )
        self.average_line.setVisible(False)
        self.plot.addItem(self.average_line, ignoreBounds=True)

        self.threshold_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(PALETTE.warning, width=1, style=Qt.PenStyle.DotLine)
        )
        self.threshold_line.setVisible(False)
        self.plot.addItem(self.threshold_line, ignoreBounds=True)

        self.crosshair_v = pg.InfiniteLine(
            angle=90, pen=pg.mkPen(PALETTE.border_strong, width=1)
        )
        self.crosshair_v.setVisible(False)
        self.plot.addItem(self.crosshair_v, ignoreBounds=True)

        self.readout = QLabel("")
        self.readout.setObjectName("Faint")
        layout.addWidget(self.readout)

        self.legend_layout = QHBoxLayout()
        self.legend_layout.setSpacing(12)
        self.legend_layout.addStretch(1)
        layout.addLayout(self.legend_layout)

        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)

    # ------------------------------------------------------------ controls ---
    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)

        self.range_group = QButtonGroup(self)
        self.range_group.setExclusive(True)
        for label, seconds in LIVE_TIME_RANGES:
            button = QPushButton(label)
            button.setObjectName("Toggle")
            button.setCheckable(True)
            button.setChecked(seconds == self._range_seconds)
            button.clicked.connect(lambda _checked, s=seconds: self.set_range(s))
            self.range_group.addButton(button)
            row.addWidget(button)

        row.addStretch(1)

        self.average_button = QPushButton("Average")
        self.average_button.setObjectName("Toggle")
        self.average_button.setCheckable(True)
        self.average_button.setChecked(True)
        self.average_button.setToolTip("Show the average of the primary target")
        self.average_button.toggled.connect(self._set_average_visible)
        row.addWidget(self.average_button)

        self.threshold_button = QPushButton("Threshold")
        self.threshold_button.setObjectName("Toggle")
        self.threshold_button.setCheckable(True)
        self.threshold_button.setToolTip("Show a fixed latency reference line")
        self.threshold_button.toggled.connect(self._set_threshold_visible)
        row.addWidget(self.threshold_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("Toggle")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip("Freeze the view - monitoring keeps running")
        self.pause_button.toggled.connect(self.set_paused)
        row.addWidget(self.pause_button)

        self.reset_button = QPushButton("Reset")
        self.reset_button.setObjectName("Toggle")
        self.reset_button.setToolTip("Return to auto-scrolling and auto-scaling")
        self.reset_button.clicked.connect(self.reset_view)
        row.addWidget(self.reset_button)
        return row

    # -------------------------------------------------------------- series ---
    def set_targets(self, targets: list[Target]) -> None:
        """Create or remove one curve per target."""
        wanted = {target.id: target for target in targets}

        for target_id in list(self._curves):
            if target_id not in wanted:
                self.plot.removeItem(self._curves.pop(target_id))
                spikes = self._spike_items.pop(target_id, None)
                if spikes is not None:
                    self.plot.removeItem(spikes)
                losses = self._loss_items.pop(target_id, None)
                if losses is not None:
                    self.plot.removeItem(losses)
                toggle = self._toggles.pop(target_id, None)
                if toggle is not None:
                    self.legend_layout.removeWidget(toggle)
                    toggle.deleteLater()
                self._visible.pop(target_id, None)
                self._targets.pop(target_id, None)

        for target_id, target in wanted.items():
            if target_id in self._curves:
                self._targets[target_id] = target
                continue
            color = QColor(target.color)
            curve = self.plot.plot(
                [], [], pen=pg.mkPen(color, width=2), name=target.name,
                connect="finite",  # gaps where probes failed
            )
            self._curves[target_id] = curve

            spikes = pg.ScatterPlotItem(
                size=9, pen=pg.mkPen(PALETTE.problem, width=1),
                brush=pg.mkBrush(QColor(PALETTE.problem)), symbol="t",
            )
            spikes.sigClicked.connect(self._on_spike_clicked)
            self.plot.addItem(spikes)
            self._spike_items[target_id] = spikes

            losses = pg.ScatterPlotItem(
                size=7, pen=pg.mkPen(PALETTE.problem, width=1),
                brush=pg.mkBrush(QColor(0, 0, 0, 0)), symbol="x",
            )
            self.plot.addItem(losses)
            self._loss_items[target_id] = losses

            toggle = QCheckBox(target.name)
            toggle.setChecked(True)
            # Qualified with the type selector so it beats the application
            # stylesheet's `QCheckBox { color: ... }` rule.
            toggle.setStyleSheet(
                f"QCheckBox {{ color: {target.color}; }}"
                f"QCheckBox::indicator:checked {{ background-color: {target.color}; "
                f"border-color: {target.color}; }}"
            )
            toggle.setToolTip(f"{target.display_host} - {target.measurement_label}")
            toggle.toggled.connect(
                lambda checked, tid=target_id: self.set_series_visible(tid, checked)
            )
            self._toggles[target_id] = toggle
            self.legend_layout.insertWidget(self.legend_layout.count() - 1, toggle)

            self._visible[target_id] = True
            self._targets[target_id] = target

    def set_series_visible(self, target_id: int | None, visible: bool) -> None:
        self._visible[target_id] = visible
        for store in (self._curves, self._spike_items, self._loss_items):
            item = store.get(target_id)
            if item is not None:
                item.setVisible(visible)

    # --------------------------------------------------------------- state ---
    def set_range(self, seconds: int) -> None:
        self._range_seconds = seconds
        self._auto_range = True
        for button in self.range_group.buttons():
            label = button.text()
            button.setChecked(
                any(label == name and seconds == value for name, value in LIVE_TIME_RANGES)
            )

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setChecked(paused)
        self.pause_button.setText("Resume" if paused else "Pause")

    @property
    def paused(self) -> bool:
        return self._paused

    def reset_view(self) -> None:
        self._auto_range = True
        self.set_paused(False)
        self.plot.enableAutoRange(axis="y")

    def _on_manual_range(self) -> None:
        # The user took control of the view; stop fighting them.
        self._auto_range = False

    def _set_average_visible(self, visible: bool) -> None:
        self._show_average = visible
        self.average_line.setVisible(visible)

    def _set_threshold_visible(self, visible: bool) -> None:
        self._show_threshold = visible
        self.threshold_line.setVisible(visible)
        self.threshold_line.setPos(self._threshold_ms)

    def set_threshold(self, value_ms: float) -> None:
        self._threshold_ms = max(1.0, value_ms)
        self.threshold_line.setPos(self._threshold_ms)

    # ------------------------------------------------------------- updates ---
    def update_from_buffers(self, buffers: dict, primary_id: int | None = None) -> None:
        """Redraw from ring buffers. Cheap enough to run at UI cadence."""
        if self._paused:
            return

        now = time.time()
        since = now - self._range_seconds
        latest_average: float | None = None

        for target_id, curve in self._curves.items():
            buffer = buffers.get(target_id)
            if buffer is None:
                curve.setData([], [])
                continue

            times, values = buffer.series(since)
            plotted = [v if v is not None else float("nan") for v in values]
            curve.setData(times, plotted)

            spikes = [s for s in buffer.spike_markers(since)]
            spike_item = self._spike_items.get(target_id)
            if spike_item is not None:
                spike_item.setData(
                    [s[0] for s in spikes],
                    [s[1] for s in spikes],
                    brush=[pg.mkBrush(QColor(PALETTE.severity_color(Severity(s[2]))))
                           for s in spikes],
                    data=[(s[0], s[1]) for s in spikes],
                )

            loss_item = self._loss_items.get(target_id)
            if loss_item is not None:
                # Failed probes are drawn on the axis so a gap is never silent.
                loss_points = [t for t, v in zip(times, values) if v is None]
                loss_item.setData(loss_points, [0.0] * len(loss_points))

            if target_id == primary_id:
                good = [v for v in values if v is not None]
                if good:
                    latest_average = sum(good) / len(good)

        if self._show_average and latest_average is not None:
            self.average_line.setPos(latest_average)
            self.average_line.setVisible(True)
            self.average_line.setToolTip(f"Average {format_latency(latest_average)}")
        elif not self._show_average:
            self.average_line.setVisible(False)

        if self._auto_range:
            self.plot.setXRange(since, now, padding=0.01)

    def clear(self) -> None:
        for curve in self._curves.values():
            curve.setData([], [])
        for item in self._spike_items.values():
            item.setData([], [])
        for item in self._loss_items.values():
            item.setData([], [])

    # --------------------------------------------------------- interaction ---
    def _on_mouse_moved(self, position) -> None:
        view_box = self.plot.getViewBox()
        if not self.plot.sceneBoundingRect().contains(position):
            self.crosshair_v.setVisible(False)
            self.readout.setText("")
            return
        point = view_box.mapSceneToView(position)
        self.crosshair_v.setPos(point.x())
        self.crosshair_v.setVisible(True)

        parts = [format_clock(point.x())]
        for target_id, curve in self._curves.items():
            if not self._visible.get(target_id, True):
                continue
            data = curve.getData()
            if data[0] is None or len(data[0]) == 0:
                continue
            value = self._nearest_value(data[0], data[1], point.x())
            if value is None:
                continue
            target = self._targets.get(target_id)
            name = target.name if target else str(target_id)
            parts.append(f"{name}: {format_latency(value)}")
        self.readout.setText("   ".join(parts))

    @staticmethod
    def _nearest_value(times, values, x: float) -> float | None:
        """Value at the sample closest to `x`, ignoring failed probes."""
        if len(times) == 0:
            return None
        import bisect

        index = bisect.bisect_left(list(times), x)
        candidates = [i for i in (index - 1, index, index + 1) if 0 <= i < len(times)]
        if not candidates:
            return None
        best = min(candidates, key=lambda i: abs(times[i] - x))
        value = values[best]
        if value is None or value != value:  # NaN check
            return None
        return float(value)

    def _on_spike_clicked(self, _item, points) -> None:
        if not points:
            return
        data = points[0].data()
        if isinstance(data, tuple) and len(data) == 2:
            self.spike_clicked.emit(data[0], data[1])
