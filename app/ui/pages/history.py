"""History page - past sessions and long-range graphs (plan sections 35-37)."""

from __future__ import annotations

import time

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import HISTORY_TIME_RANGES, CATEGORY_COLORS
from app.services.monitoring_service import MonitoringService
from app.storage.models import SessionSummary
from app.ui.theme import PALETTE, SPACING, monospace_family
from app.ui.widgets.event_log import EventLogCard, show_event_details
from app.ui.widgets.live_graph import TimeAxis
from app.ui.widgets.status_card import Card
from app.utils.logger import get_logger
from app.utils.time import format_datetime, format_duration, format_latency

log = get_logger("ui.history")


class HistoryPage(QWidget):
    """Browse stored sessions, their statistics and their events."""

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.repository = service.repository
        self._summaries: dict[int, SessionSummary] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING + 4, SPACING + 4, SPACING + 4, SPACING + 4)
        layout.setSpacing(SPACING)

        header = QHBoxLayout()
        title = QLabel("HISTORY")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.range_combo = QComboBox()
        for label, seconds in HISTORY_TIME_RANGES:
            self.range_combo.addItem(label, seconds)
        self.range_combo.setCurrentIndex(3)  # 24 hours
        self.range_combo.currentIndexChanged.connect(self._refresh_graph)
        header.addWidget(QLabel("Range:"))
        header.addWidget(self.range_combo)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload)
        header.addWidget(self.refresh_button)

        self.export_button = QPushButton("Export session CSV")
        self.export_button.clicked.connect(self._export_session)
        header.addWidget(self.export_button)

        self.report_button = QPushButton("Session report")
        self.report_button.clicked.connect(self._session_report)
        header.addWidget(self.report_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_session)
        header.addWidget(self.delete_button)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Sessions table
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(SPACING)

        sessions_card = Card("Sessions")
        self.sessions_table = QTableWidget(0, 6)
        self.sessions_table.setHorizontalHeaderLabels(
            ["Started", "Duration", "Health", "Avg", "Loss", "Spikes"]
        )
        self.sessions_table.verticalHeader().setVisible(False)
        self.sessions_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.sessions_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.sessions_table.setAlternatingRowColors(True)
        self.sessions_table.itemSelectionChanged.connect(self._on_session_selected)
        self.sessions_table.setColumnWidth(0, 150)
        sessions_card.add(self.sessions_table, 1)
        left_layout.addWidget(sessions_card, 1)
        splitter.addWidget(left)

        # Detail side
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(SPACING)

        graph_card = Card("Historical latency")
        self.plot = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.plot.setLabel("left", "Latency", units="ms")
        self.plot.showGrid(x=True, y=True, alpha=0.16)
        self.plot.setMenuEnabled(False)
        self.plot.setMinimumHeight(200)
        self.plot.addLegend(offset=(-10, 10), labelTextSize="7pt",
                            brush=pg.mkBrush(PALETTE.surface), pen=None)
        graph_card.add(self.plot, 1)
        self.graph_note = QLabel(
            "Solid line: average per bucket. Dashed line of the same colour: 95th "
            "percentile. A low average can hide severe spikes, which is why both "
            "are drawn."
        )
        self.graph_note.setObjectName("Faint")
        self.graph_note.setWordWrap(True)
        graph_card.add(self.graph_note)
        right_layout.addWidget(graph_card, 2)

        stats_card = Card("Session statistics")
        self.stats_label = QLabel("Select a session to see its statistics.")
        self.stats_label.setStyleSheet(f"font-family: '{monospace_family()}';")
        self.stats_label.setWordWrap(True)
        self.stats_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        stats_card.add(self.stats_label, 1)
        right_layout.addWidget(stats_card, 1)

        self.events_card = EventLogCard("Session events")
        self.events_card.event_activated.connect(
            lambda event: show_event_details(event, self)
        )
        right_layout.addWidget(self.events_card, 2)

        splitter.addWidget(right)
        splitter.setSizes([420, 780])

    # -------------------------------------------------------------- loading --
    def reload(self) -> None:
        self.repository.flush()
        sessions = self.repository.list_sessions(limit=200)
        self.sessions_table.setRowCount(len(sessions))
        self._summaries.clear()

        for row, session in enumerate(sessions):
            if session.id is None:
                continue
            summary = self.repository.session_summary(session.id)
            if summary is None:
                continue
            self._summaries[session.id] = summary

            values = [
                format_datetime(session.start_time),
                format_duration(session.duration_s),
                f"{summary.health_score} {summary.status.label}",
                format_latency(summary.average_ms),
                f"{summary.loss_fraction * 100:.2f}%",
                str(summary.spike_count),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, session.id)
                if column == 2:
                    item.setForeground(
                        self._score_color(summary.health_score)
                    )
                self.sessions_table.setItem(row, column, item)

        if self.sessions_table.rowCount():
            self.sessions_table.selectRow(0)
        else:
            self.stats_label.setText("No sessions recorded yet.")
            self.plot.clear()
            self.events_card.clear()

    @staticmethod
    def _score_color(score: int):
        from PySide6.QtGui import QColor

        if score >= 80:
            return QColor(PALETTE.healthy)
        if score >= 50:
            return QColor(PALETTE.warning)
        return QColor(PALETTE.problem)

    def selected_session_id(self) -> int | None:
        items = self.sessions_table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _on_session_selected(self) -> None:
        session_id = self.selected_session_id()
        if session_id is None:
            return
        summary = self._summaries.get(session_id)
        if summary is None:
            return

        lines = [
            f"Session:   {summary.session.name or session_id}",
            f"Started:   {format_datetime(summary.session.start_time)}",
            f"Ended:     "
            f"{format_datetime(summary.session.end_time) if summary.session.end_time else 'ongoing'}",
            f"Duration:  {format_duration(summary.session.duration_s)}",
            "",
            f"Health:    {summary.health_score} / 100  ({summary.status.label})",
            f"Samples:   {summary.sample_count}",
            "",
            f"Average:   {format_latency(summary.average_ms)}",
            f"Median:    {format_latency(summary.median_ms)}",
            f"P95:       {format_latency(summary.p95_ms)}",
            f"P99:       {format_latency(summary.p99_ms)}",
            f"Min:       {format_latency(summary.min_ms)}",
            f"Max:       {format_latency(summary.max_ms)}",
            "",
            f"Loss:      {summary.loss_fraction * 100:.2f}%",
            f"Spikes:    {summary.spike_count}",
            f"Events:    {summary.event_count}",
        ]
        self.stats_label.setText("\n".join(lines))

        events = self.repository.get_events(session_id=session_id, limit=300)
        self.events_card.set_events(list(reversed(events)))
        self._refresh_graph()

    def _refresh_graph(self) -> None:
        session_id = self.selected_session_id()
        self.plot.clear()
        if session_id is None:
            return
        summary = self._summaries.get(session_id)
        if summary is None:
            return

        seconds = self.range_combo.currentData() or 86400
        end = summary.session.end_time or time.time()
        start = max(summary.session.start_time, end - seconds)

        targets = {t.id: t for t in self.repository.list_targets()}
        drawn = 0
        for target_id in self.repository.session_target_ids(session_id):
            series = self.repository.get_downsampled_series(target_id, start, end, buckets=400)
            if not series:
                continue
            target = targets.get(target_id)
            name = target.name if target else f"Target {target_id}"
            color = CATEGORY_COLORS.get(
                target.category if target else "", PALETTE.text_muted
            )
            times = [row[0] for row in series]
            averages = [row[1] if row[1] is not None else float("nan") for row in series]
            p95s = [row[2] if row[2] is not None else float("nan") for row in series]

            self.plot.plot(times, averages, pen=pg.mkPen(color, width=2),
                           name=name, connect="finite")
            # The dashed p95 line shares the target's colour and is explained by
            # the caption, so naming it too would double the legend's height.
            self.plot.plot(
                times, p95s,
                pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine),
                connect="finite",
            )
            drawn += 1

        if drawn:
            self.plot.setXRange(start, end, padding=0.01)
            self.plot.enableAutoRange(axis="y")

    # ------------------------------------------------------------- actions --
    def _export_session(self) -> None:
        session_id = self.selected_session_id()
        if session_id is None:
            QMessageBox.information(self, "Export", "Select a session first.")
            return
        from app.services.export_service import export_samples_csv, suggested_filename

        path, _ = QFileDialog.getSaveFileName(
            self, "Export session samples",
            suggested_filename(f"session_{session_id}", "csv"),
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        rows = export_samples_csv(self.repository, path, session_id=session_id)
        QMessageBox.information(self, "Export", f"Exported {rows} samples to:\n{path}")

    def _session_report(self) -> None:
        session_id = self.selected_session_id()
        if session_id is None:
            QMessageBox.information(self, "Report", "Select a session first.")
            return
        from app.services.report_service import build_session_report, context_from_session

        try:
            context = context_from_session(self.repository, session_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Report", str(exc))
            return

        text = build_session_report(context, title="SESSION REPORT")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Session report")
        dialog.setText("Session report generated.")
        dialog.setDetailedText(text)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Close
        )
        if dialog.exec() == QMessageBox.StandardButton.Save:
            from app.services.export_service import export_text, suggested_filename

            path, _ = QFileDialog.getSaveFileName(
                self, "Save report", suggested_filename(f"session_{session_id}", "txt"),
                "Text files (*.txt)",
            )
            if path:
                export_text(text, path)

    def _delete_session(self) -> None:
        session_id = self.selected_session_id()
        if session_id is None:
            return
        confirm = QMessageBox.question(
            self, "Delete session",
            "Delete this session and all of its samples and events?\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.repository.delete_session(session_id)
        self.reload()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        self.reload()
