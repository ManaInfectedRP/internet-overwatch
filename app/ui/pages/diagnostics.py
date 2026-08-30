"""Diagnostics page - "Why am I lagging?" (plan sections 15-19, 29-34, 69).

Layer cards answer the three-layer question, and the tools below run the heavy
one-off diagnostics. Every long-running tool executes on a worker thread and
streams results back through signals, so the window never freezes.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import NodeStatus, TargetCategory
from app.network.dns import check_resolvers, system_resolution_time
from app.network.interfaces import collect_network_info
from app.network.throughput import BufferbloatTest
from app.network.traceroute import TRACEROUTE_CAVEAT, Hop, run_traceroute
from app.network.wifi import get_wifi_info
from app.services.monitoring_service import MonitoringService
from app.services.report_service import (
    build_isp_report,
    build_session_report,
    context_from_monitor,
)
from app.ui.theme import PALETTE, SPACING, monospace_family
from app.ui.widgets.status_card import Card, StatusChip
from app.utils.logger import get_logger
from app.utils.time import format_bps, format_latency

log = get_logger("ui.diagnostics")


class Worker(QObject):
    """Runs a callable on a thread and reports back on the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str, float)
    partial = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run(self, func, *args, **kwargs) -> bool:
        if self.busy:
            return False
        self.stop_event.clear()

        def target() -> None:
            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - surfaced in the UI
                log.warning("Diagnostic task failed: %s", exc, exc_info=True)
                self.failed.emit(str(exc))
                return
            self.finished.emit(result)

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self.stop_event.set()


class LayerCard(Card):
    """One of the three layer sections."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        header = QHBoxLayout()
        self.summary = QLabel("Waiting for measurements...")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {PALETTE.text}; font-weight: 600;")
        self.chip = StatusChip()
        header.addWidget(self.summary, 1)
        header.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        self.add_layout(header)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("Muted")
        self.detail.setStyleSheet(f"font-family: '{monospace_family()}';")
        self.add(self.detail)
        self.body().addStretch(1)

    def update_layer(self, report) -> None:
        self.chip.set_status(report.status)
        self.summary.setText(report.summary)
        self.summary.setStyleSheet(
            f"color: {PALETTE.status_color(report.status)}; font-weight: 600;"
        )
        self.detail.setText("\n".join(report.lines))


class DiagnosticsPage(QWidget):
    """Layer analysis plus the manual diagnostic tools."""

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._trace_worker = Worker(self)
        self._dns_worker = Worker(self)
        self._bloat_worker = Worker(self)
        self._bufferbloat: BufferbloatTest | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SPACING + 4, SPACING + 4, SPACING + 4, SPACING + 4)
        layout.setSpacing(SPACING)

        title = QLabel("DIAGNOSTICS")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        subtitle = QLabel(
            "Comparing the local network, the public internet and your destination "
            "is what makes it possible to say where a problem starts."
        )
        subtitle.setObjectName("Faint")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        layers = QHBoxLayout()
        layers.setSpacing(SPACING)
        self.local_card = LayerCard("Local network")
        self.internet_card = LayerCard("Internet")
        self.destination_card = LayerCard("Destination")
        for card in (self.local_card, self.internet_card, self.destination_card):
            layers.addWidget(card, 1)
        layout.addLayout(layers)

        self.diagnosis_card = Card("Diagnosis")
        self.diagnosis_headline = QLabel("Waiting for measurements...")
        self.diagnosis_headline.setWordWrap(True)
        self.diagnosis_headline.setStyleSheet("font-size: 13pt; font-weight: 700;")
        self.diagnosis_card.add(self.diagnosis_headline)
        self.diagnosis_confidence = QLabel("")
        self.diagnosis_confidence.setObjectName("Muted")
        self.diagnosis_card.add(self.diagnosis_confidence)
        self.diagnosis_detail = QLabel("")
        self.diagnosis_detail.setWordWrap(True)
        self.diagnosis_detail.setObjectName("Muted")
        self.diagnosis_card.add(self.diagnosis_detail)
        self.diagnosis_evidence = QLabel("")
        self.diagnosis_evidence.setWordWrap(True)
        self.diagnosis_evidence.setObjectName("Faint")
        self.diagnosis_card.add(self.diagnosis_evidence)
        layout.addWidget(self.diagnosis_card)

        self.tools = QTabWidget()
        self.tools.addTab(self._build_traceroute_tab(), "Traceroute")
        self.tools.addTab(self._build_dns_tab(), "DNS")
        self.tools.addTab(self._build_network_tab(), "Adapter && Wi-Fi")
        self.tools.addTab(self._build_bufferbloat_tab(), "Bufferbloat")
        self.tools.addTab(self._build_report_tab(), "Reports")
        layout.addWidget(self.tools, 1)

    # ----------------------------------------------------------- traceroute --
    def _build_traceroute_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.trace_input = QLineEdit()
        self.trace_input.setPlaceholderText("Host or IP to trace, e.g. 1.1.1.1")
        self.trace_button = QPushButton("Run traceroute")
        self.trace_button.setObjectName("Primary")
        self.trace_button.clicked.connect(self._run_traceroute)
        self.trace_save_button = QPushButton("Save to history")
        self.trace_save_button.setEnabled(False)
        self.trace_save_button.clicked.connect(self._save_traceroute)
        row.addWidget(self.trace_input, 1)
        row.addWidget(self.trace_button)
        row.addWidget(self.trace_save_button)
        layout.addLayout(row)

        self.trace_table = QTableWidget(0, 4)
        self.trace_table.setHorizontalHeaderLabels(["Hop", "Host", "Best", "Average"])
        self.trace_table.horizontalHeader().setStretchLastSection(True)
        self.trace_table.setColumnWidth(0, 50)
        self.trace_table.setColumnWidth(1, 320)
        self.trace_table.verticalHeader().setVisible(False)
        layout.addWidget(self.trace_table, 1)

        caveat = QLabel(TRACEROUTE_CAVEAT)
        caveat.setWordWrap(True)
        caveat.setObjectName("Faint")
        layout.addWidget(caveat)

        self.trace_status = QLabel("")
        self.trace_status.setObjectName("Muted")
        layout.addWidget(self.trace_status)

        self._trace_worker.finished.connect(self._on_traceroute_done)
        self._trace_worker.failed.connect(self._on_tool_failed)
        self._trace_result = None
        return widget

    def _run_traceroute(self) -> None:
        target = self.trace_input.text().strip()
        if not target:
            target = self._default_trace_target()
            self.trace_input.setText(target)
        if not target:
            QMessageBox.information(self, "Traceroute", "Enter a host or IP to trace.")
            return
        if self._trace_worker.busy:
            return

        self.trace_table.setRowCount(0)
        self.trace_button.setEnabled(False)
        self.trace_save_button.setEnabled(False)
        self.trace_status.setText(f"Tracing route to {target}...")
        settings = self.service.settings.advanced
        self._trace_worker.run(
            run_traceroute,
            target,
            30,
            self.service.settings.monitoring.timeout_ms * 2,
            settings.traceroute_command,
            None,
            self._trace_worker.stop_event,
        )

    def _default_trace_target(self) -> str:
        for target in self.service.targets():
            if target.category == TargetCategory.CUSTOM.value:
                return target.host
        for target in self.service.targets():
            if target.category == TargetCategory.INTERNET.value:
                return target.host
        return "1.1.1.1"

    def _on_traceroute_done(self, result) -> None:
        self.trace_button.setEnabled(True)
        self._trace_result = result
        if result.error:
            self.trace_status.setText(f"Traceroute failed: {result.error}")
            return
        self.trace_table.setRowCount(len(result.hops))
        for row, hop in enumerate(result.hops):
            self._set_hop_row(row, hop)
        self.trace_status.setText(
            f"{len(result.hops)} hops to {result.target}"
            + ("" if result.completed else " (stopped early)")
        )
        self.trace_save_button.setEnabled(bool(result.hops))

    def _set_hop_row(self, row: int, hop: Hop) -> None:
        values = [
            str(hop.number),
            hop.display_host,
            format_latency(hop.best_ms),
            format_latency(hop.average_ms),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if not hop.responded:
                item.setForeground(self.palette().mid())
            self.trace_table.setItem(row, column, item)

    def _save_traceroute(self) -> None:
        if self._trace_result is None:
            return
        session_id = (
            self.service.monitor.session.id if self.service.monitor.session else None
        )
        self.service.repository.save_traceroute(
            self._trace_result.target,
            [hop.to_dict() for hop in self._trace_result.hops],
            session_id,
            self._trace_result.timestamp,
        )
        self.trace_status.setText("Traceroute saved - it will be included in ISP reports.")
        self.trace_save_button.setEnabled(False)

    # ------------------------------------------------------------------ dns --
    def _build_dns_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.dns_resolvers = QLineEdit(
            ", ".join(self.service.settings.advanced.dns_resolvers)
        )
        self.dns_resolvers.setPlaceholderText("Resolvers, comma separated")
        self.dns_host = QLineEdit(self.service.settings.advanced.dns_probe_host)
        self.dns_host.setPlaceholderText("Hostname to resolve")
        self.dns_host.setMaximumWidth(200)
        self.dns_button = QPushButton("Test DNS")
        self.dns_button.setObjectName("Primary")
        self.dns_button.clicked.connect(self._run_dns_test)
        row.addWidget(self.dns_resolvers, 1)
        row.addWidget(self.dns_host)
        row.addWidget(self.dns_button)
        layout.addLayout(row)

        self.dns_table = QTableWidget(0, 4)
        self.dns_table.setHorizontalHeaderLabels(
            ["Resolver", "Response time", "Failures", "Status"]
        )
        self.dns_table.horizontalHeader().setStretchLastSection(True)
        self.dns_table.verticalHeader().setVisible(False)
        layout.addWidget(self.dns_table, 1)

        self.dns_status = QLabel("")
        self.dns_status.setObjectName("Muted")
        self.dns_status.setWordWrap(True)
        layout.addWidget(self.dns_status)

        self._dns_worker.finished.connect(self._on_dns_done)
        self._dns_worker.failed.connect(self._on_tool_failed)
        return widget

    def _run_dns_test(self) -> None:
        if self._dns_worker.busy:
            return
        resolvers = [r.strip() for r in self.dns_resolvers.text().split(",") if r.strip()]
        if not resolvers:
            QMessageBox.information(self, "DNS", "Enter at least one resolver.")
            return
        host = self.dns_host.text().strip() or "example.com"
        self.dns_button.setEnabled(False)
        self.dns_status.setText("Querying resolvers...")

        def task():
            health = check_resolvers(resolvers, host, samples=3,
                                     timeout_ms=self.service.settings.monitoring.timeout_ms * 2)
            return health, system_resolution_time(host)

        self._dns_worker.run(task)

    def _on_dns_done(self, result) -> None:
        health, system_ms = result
        self.dns_button.setEnabled(True)
        self.dns_table.setRowCount(len(health))
        for row, entry in enumerate(health):
            status = NodeStatus.HEALTHY if entry.healthy else (
                NodeStatus.WARNING if entry.latency_ms else NodeStatus.PROBLEM
            )
            values = [
                entry.resolver,
                format_latency(entry.latency_ms),
                f"{entry.failures} / {entry.samples}",
                f"{status.symbol} {status.label}",
            ]
            for column, value in enumerate(values):
                self.dns_table.setItem(row, column, QTableWidgetItem(value))
        self.dns_status.setText(
            f"System resolver (including its cache): {format_latency(system_ms)}. "
            "Slow DNS delays connections but does not raise in-game latency once "
            "a connection is established."
        )

    # -------------------------------------------------------- adapter / wifi --
    def _build_network_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.network_button = QPushButton("Refresh")
        self.network_button.clicked.connect(self.refresh_network_info)
        row.addWidget(self.network_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.network_text = QPlainTextEdit()
        self.network_text.setReadOnly(True)
        self.network_text.setStyleSheet(f"font-family: '{monospace_family()}';")
        layout.addWidget(self.network_text, 1)

        self.wifi_warning = QLabel("")
        self.wifi_warning.setWordWrap(True)
        self.wifi_warning.setObjectName("Faint")
        layout.addWidget(self.wifi_warning)
        return widget

    def refresh_network_info(self) -> None:
        info = collect_network_info()
        wifi = get_wifi_info()
        lines = [
            f"Hostname:  {info.hostname}",
            f"Gateway:   {info.gateway or 'not detected'}",
            f"Gateway v6:{info.gateway_ipv6 or ' -'}",
            f"DNS:       {', '.join(info.dns_servers) or 'unknown'}",
            f"IPv4:      {'available' if info.has_ipv4 else 'unavailable'}",
            f"IPv6:      {'available' if info.has_ipv6 else 'unavailable'}",
            "",
            "ADAPTERS",
        ]
        for iface in info.interfaces:
            marker = " *" if iface.is_default else "  "
            lines.append(
                f"{marker}{iface.name}  [{iface.connection_type}] "
                f"{'up' if iface.is_up else 'down'}"
            )
            lines.append(f"    IPv4: {iface.ipv4 or '-'}   IPv6: {iface.ipv6 or '-'}")
            lines.append(
                f"    MAC:  {iface.mac or '-'}   Link: {iface.link_speed_text}   "
                f"MTU: {iface.mtu or '-'}"
            )

        lines.append("")
        lines.append("WI-FI")
        if wifi.available and wifi.connected:
            lines.extend([
                f"  SSID:       {wifi.ssid or '-'}",
                f"  BSSID:      {wifi.bssid or '-'}",
                f"  Signal:     {wifi.signal_percent if wifi.signal_percent is not None else '-'}%",
                f"  Channel:    {wifi.channel or '-'}  ({wifi.band or 'band unknown'})",
                f"  Radio:      {wifi.radio_type or '-'}",
                f"  Link speed: {wifi.link_speed_text}",
            ])
        else:
            lines.append(f"  {wifi.note or 'Not connected to Wi-Fi'}")

        self.network_text.setPlainText("\n".join(lines))
        warnings = wifi.warnings
        self.wifi_warning.setText(" ".join(warnings) if warnings else "")
        self.wifi_warning.setStyleSheet(
            f"color: {PALETTE.warning};" if warnings else ""
        )

    # ---------------------------------------------------------- bufferbloat --
    def _build_bufferbloat_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        explain = QLabel(
            "Measures how much latency rises while the connection is saturated. "
            "It deliberately generates traffic, so do not run it while gaming."
        )
        explain.setWordWrap(True)
        explain.setObjectName("Muted")
        layout.addWidget(explain)

        row = QHBoxLayout()
        self.bloat_button = QPushButton("Run bufferbloat test")
        self.bloat_button.setObjectName("Primary")
        self.bloat_button.clicked.connect(self._run_bufferbloat)
        self.bloat_cancel = QPushButton("Cancel")
        self.bloat_cancel.setEnabled(False)
        self.bloat_cancel.clicked.connect(self._cancel_bufferbloat)
        row.addWidget(self.bloat_button)
        row.addWidget(self.bloat_cancel)
        row.addStretch(1)
        layout.addLayout(row)

        self.bloat_progress = QProgressBar()
        self.bloat_progress.setRange(0, 100)
        self.bloat_progress.setVisible(False)
        layout.addWidget(self.bloat_progress)

        self.bloat_result = QPlainTextEdit()
        self.bloat_result.setReadOnly(True)
        self.bloat_result.setStyleSheet(f"font-family: '{monospace_family()}';")
        layout.addWidget(self.bloat_result, 1)

        self._bloat_worker.finished.connect(self._on_bufferbloat_done)
        self._bloat_worker.failed.connect(self._on_tool_failed)
        self._bloat_worker.progress.connect(self._on_bufferbloat_progress)
        return widget

    def _run_bufferbloat(self) -> None:
        if self._bloat_worker.busy:
            return
        host = self._default_trace_target()
        self._bufferbloat = BufferbloatTest(
            host, timeout_ms=self.service.settings.monitoring.timeout_ms
        )
        self.bloat_button.setEnabled(False)
        self.bloat_cancel.setEnabled(True)
        self.bloat_progress.setVisible(True)
        self.bloat_progress.setValue(0)
        self.bloat_result.setPlainText(f"Testing against {host}...")

        test = self._bufferbloat

        def progress(phase: str, fraction: float) -> None:
            self._bloat_worker.progress.emit(phase, fraction)

        self._bloat_worker.run(test.run, progress)

    def _cancel_bufferbloat(self) -> None:
        if self._bufferbloat is not None:
            self._bufferbloat.cancel()
        self.bloat_cancel.setEnabled(False)

    def _on_bufferbloat_progress(self, phase: str, fraction: float) -> None:
        self.bloat_progress.setValue(int(fraction * 100))
        self.bloat_progress.setFormat(f"{phase}  %p%")

    def _on_bufferbloat_done(self, result) -> None:
        self.bloat_button.setEnabled(True)
        self.bloat_cancel.setEnabled(False)
        self.bloat_progress.setVisible(False)
        if result.error:
            self.bloat_result.setPlainText(result.error)
            return
        lines = [
            f"Idle latency:            {format_latency(result.idle_latency_ms)}",
            f"Latency under download:  {format_latency(result.download_latency_ms)}"
            f"   (+{format_latency(result.download_increase_ms)})",
            f"Latency under upload:    {format_latency(result.upload_latency_ms)}"
            f"   (+{format_latency(result.upload_increase_ms)})",
            "",
            f"Download throughput:     {format_bps(result.download_mbps * 1e6)}",
            f"Upload throughput:       {format_bps(result.upload_mbps * 1e6)}",
            "",
            f"Grade: {result.grade}   {result.verdict}",
            "",
            "Bufferbloat is latency caused by oversized buffers filling during heavy",
            "transfers. It is usually fixed on the router (SQM / smart queue), not by",
            "the ISP.",
        ]
        self.bloat_result.setPlainText("\n".join(lines))

    # -------------------------------------------------------------- reports --
    def _build_report_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.report_button = QPushButton("Generate report")
        self.report_button.clicked.connect(self._generate_report)
        self.isp_button = QPushButton("Create ISP report")
        self.isp_button.setObjectName("Primary")
        self.isp_button.setToolTip(
            "Evidence-oriented report suitable for attaching to a support ticket"
        )
        self.isp_button.clicked.connect(self._generate_isp_report)
        self.save_report_button = QPushButton("Save as...")
        self.save_report_button.clicked.connect(self._save_report)
        self.copy_report_button = QPushButton("Copy")
        self.copy_report_button.clicked.connect(self._copy_report)
        for button in (self.report_button, self.isp_button, self.save_report_button,
                       self.copy_report_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self.report_text = QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.report_text.setStyleSheet(f"font-family: '{monospace_family()}';")
        self.report_text.setPlaceholderText(
            "Generate a report to summarise this session's measurements."
        )
        layout.addWidget(self.report_text, 1)
        return widget

    def _report_context(self):
        context = context_from_monitor(self.service.monitor)
        info = collect_network_info()
        wifi = get_wifi_info()
        context.network_info = {
            "Gateway": info.gateway or "not detected",
            "DNS servers": ", ".join(info.dns_servers) or "unknown",
            "Adapter": info.default_interface.name if info.default_interface else "unknown",
            "Connection type": (
                info.default_interface.connection_type if info.default_interface else "unknown"
            ),
            "Link speed": (
                info.default_interface.link_speed_text if info.default_interface else "unknown"
            ),
        }
        if wifi.available and wifi.connected:
            context.wifi_info = {
                "SSID": wifi.ssid or "-",
                "Signal": f"{wifi.signal_percent}%" if wifi.signal_percent is not None else "-",
                "Channel": f"{wifi.channel or '-'} ({wifi.band or 'unknown band'})",
                "Link speed": wifi.link_speed_text,
            }
        traces = self.service.repository.get_traceroutes(limit=3)
        context.traceroutes = traces
        return context

    def _generate_report(self) -> None:
        self.report_text.setPlainText(build_session_report(self._report_context()))

    def _generate_isp_report(self) -> None:
        self.report_text.setPlainText(build_isp_report(self._report_context()))

    def _save_report(self) -> None:
        text = self.report_text.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Report", "Generate a report first.")
            return
        from app.services.export_service import export_text, suggested_filename

        path, _ = QFileDialog.getSaveFileName(
            self, "Save report", suggested_filename("overwatch_report", "txt"),
            "Text files (*.txt);;All files (*)",
        )
        if path:
            if export_text(text, path):
                self.tools.setTabToolTip(4, f"Last saved to {path}")
            else:
                QMessageBox.warning(self, "Report", "Could not write that file.")

    def _copy_report(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = self.report_text.toPlainText()
        if text.strip():
            QApplication.clipboard().setText(text)

    # -------------------------------------------------------------- shared --
    def _on_tool_failed(self, message: str) -> None:
        self.trace_button.setEnabled(True)
        self.dns_button.setEnabled(True)
        self.bloat_button.setEnabled(True)
        self.bloat_cancel.setEnabled(False)
        self.bloat_progress.setVisible(False)
        QMessageBox.warning(self, "Diagnostic failed", message)

    def refresh(self) -> None:
        report = self.service.report()
        self.local_card.update_layer(report.local)
        self.internet_card.update_layer(report.internet)
        self.destination_card.update_layer(report.destination)

        diagnosis = report.diagnosis
        self.diagnosis_headline.setText(diagnosis.headline)
        color = PALETTE.text if diagnosis.layer == "none" else PALETTE.warning
        if diagnosis.layer == "none":
            color = PALETTE.healthy
        self.diagnosis_headline.setStyleSheet(
            f"color: {color}; font-size: 13pt; font-weight: 700;"
        )
        self.diagnosis_confidence.setText(
            f"{diagnosis.confidence.wording} · Confidence: {diagnosis.confidence.label}"
        )
        self.diagnosis_detail.setText(diagnosis.detail)
        self.diagnosis_evidence.setText(
            "\n".join(f"• {item}" for item in diagnosis.evidence)
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        if not self.network_text.toPlainText():
            self.refresh_network_info()
