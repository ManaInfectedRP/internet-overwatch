"""Main window: sidebar, page stack, status bar, tray and overlay.

Plan sections 6, 7, 45, 46, 55, 85, 91. The window owns the page widgets and
drives them from a single timer tick, so all six pages share one coalesced
update instead of each polling the monitor.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.config.defaults import APP_NAME, NodeStatus
from app.services.monitoring_service import MonitoringService
from app.storage.models import Event, Incident
from app.ui.pages.diagnostics import DiagnosticsPage
from app.ui.pages.history import HistoryPage
from app.ui.pages.live_monitor import LiveMonitorPage
from app.ui.pages.overview import OverviewPage
from app.ui.pages.settings import SettingsPage
from app.ui.pages.targets import TargetsPage
from app.ui.theme import PALETTE, SPACING, stylesheet
from app.ui.widgets.event_log import IncidentDialog, show_event_details
from app.ui.widgets.overlay import OverlayWindow
from app.utils.assets import app_icon, logo_pixmap
from app.utils.logger import get_logger
from app.utils.time import format_duration

log = get_logger("ui.main_window")

PAGES = [
    ("Overview", "overview"),
    ("Live Monitor", "live"),
    ("Diagnostics", "diagnostics"),
    ("History", "history"),
    ("Targets", "targets"),
    ("Settings", "settings"),
]

COMPACT_SIDEBAR_WIDTH = 1000


class Sidebar(QWidget):
    """Fixed navigation column."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(196)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(14, 16, 12, 6)
        brand_layout.setSpacing(10)

        logo = logo_pixmap(34)
        if logo is not None:
            mark = QLabel()
            mark.setPixmap(logo)
            mark.setFixedSize(34, 34)
            mark.setScaledContents(True)
            brand_layout.addWidget(mark)

        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel("INTERNET\nOVERWATCH")
        name.setStyleSheet(
            f"color: {PALETTE.text}; font-size: 11pt; font-weight: 700; line-height: 1.1;"
        )
        text.addWidget(name)
        brand_layout.addLayout(text, 1)
        layout.addWidget(brand)

        tagline = QLabel("Monitor. Analyze. Protect. Play.")
        tagline.setObjectName("SidebarSubtitle")
        tagline.setWordWrap(True)
        layout.addWidget(tagline)

        self.buttons: dict[str, QPushButton] = {}
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for label, key in PAGES:
            button = QPushButton(label)
            button.setObjectName("SidebarButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.buttons[key] = button
            self.group.addButton(button)
            layout.addWidget(button)

        layout.addStretch(1)

        self.status_dot = QLabel("○  IDLE")
        self.status_dot.setStyleSheet(
            f"color: {PALETTE.text_faint}; padding: 10px 16px; font-weight: 600;"
        )
        layout.addWidget(self.status_dot)

        version = QLabel(f"v{__version__}")
        version.setObjectName("SidebarSubtitle")
        layout.addWidget(version)

    def set_state(self, running: bool, paused: bool, simulating: bool,
                  status: NodeStatus) -> None:
        if simulating:
            text, color = "◆  SIMULATION", PALETTE.accent
        elif paused:
            text, color = "❚❚  PAUSED", PALETTE.warning
        elif running:
            text, color = f"{status.symbol}  MONITORING", PALETTE.status_color(status)
        else:
            text, color = "○  IDLE", PALETTE.text_faint
        self.status_dot.setText(text)
        self.status_dot.setStyleSheet(
            f"color: {color}; padding: 10px 16px; font-weight: 600;"
        )

    def set_compact(self, compact: bool) -> None:
        self.setFixedWidth(64 if compact else 196)
        for (label, key) in PAGES:
            button = self.buttons[key]
            button.setText("" if compact else label)
            button.setToolTip(label if compact else "")


class MainWindow(QMainWindow):
    """Application shell."""

    def __init__(self, service: MonitoringService) -> None:
        super().__init__()
        self.service = service
        self.settings = service.settings
        self.overlay: OverlayWindow | None = None
        self.tray: QSystemTrayIcon | None = None
        self._compact = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(QSize(980, 620))
        self.resize(1440, 900)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        root.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._build_topbar())

        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        root.addWidget(right, 1)

        self.overview_page = OverviewPage(service)
        self.live_page = LiveMonitorPage(service)
        self.diagnostics_page = DiagnosticsPage(service)
        self.history_page = HistoryPage(service)
        self.targets_page = TargetsPage(service)
        self.settings_page = SettingsPage(service)

        self.pages = {
            "overview": self.overview_page,
            "live": self.live_page,
            "diagnostics": self.diagnostics_page,
            "history": self.history_page,
            "targets": self.targets_page,
            "settings": self.settings_page,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        for key, button in self.sidebar.buttons.items():
            button.clicked.connect(lambda _checked, k=key: self.show_page(k))
        self.sidebar.buttons["overview"].setChecked(True)

        self.setStatusBar(QStatusBar())
        self.status_message = QLabel("Ready")
        self.statusBar().addWidget(self.status_message, 1)
        self.status_detail = QLabel("")
        self.statusBar().addPermanentWidget(self.status_detail)

        self._connect_signals()
        self._build_shortcuts()
        self._setup_tray()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.settings.ui_refresh_ms())
        self.refresh_timer.timeout.connect(self._refresh_active_page)
        self.refresh_timer.start()

        self.refresh_targets()
        self._update_controls()

    # -------------------------------------------------------------- topbar --
    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            f"background-color: {PALETTE.surface}; "
            f"border-bottom: 1px solid {PALETTE.border};"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(SPACING + 4, 8, SPACING + 4, 8)
        layout.setSpacing(8)

        self.page_title = QLabel("Overview")
        self.page_title.setStyleSheet(
            f"color: {PALETTE.text}; font-size: 12pt; font-weight: 700;"
        )
        layout.addWidget(self.page_title)
        layout.addStretch(1)

        self.connectivity_label = QLabel("")
        self.connectivity_label.setStyleSheet(f"color: {PALETTE.problem}; font-weight: 600;")
        layout.addWidget(self.connectivity_label)

        self.start_button = QPushButton("Start monitoring")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.toggle_monitoring)
        layout.addWidget(self.start_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.toggled.connect(self.toggle_pause)
        layout.addWidget(self.pause_button)

        self.overlay_button = QPushButton("Overlay")
        self.overlay_button.setCheckable(True)
        self.overlay_button.setToolTip("Small always-on-top metrics window (Ctrl+O)")
        self.overlay_button.toggled.connect(self.set_overlay_visible)
        layout.addWidget(self.overlay_button)

        self.gaming_button = QPushButton("Gaming mode")
        self.gaming_button.setCheckable(True)
        self.gaming_button.setToolTip(
            "Fewer UI updates, no heavy diagnostics, game target prioritised (Ctrl+G)"
        )
        self.gaming_button.toggled.connect(self.set_gaming_mode)
        layout.addWidget(self.gaming_button)
        return bar

    def _connect_signals(self) -> None:
        service = self.service
        service.event_logged.connect(self._on_event)
        service.incident_detected.connect(self._on_incident)
        service.state_changed.connect(self._on_state_changed)
        service.targets_changed.connect(self.refresh_targets)
        service.notification_requested.connect(self._on_notification)

        self.overview_page.event_selected.connect(
            lambda event: show_event_details(event, self)
        )
        self.overview_page.incident_selected.connect(self._show_incident)

        self.targets_page.targets_changed.connect(self.refresh_targets)
        self.settings_page.settings_applied.connect(self._on_settings_applied)
        self.settings_page.simulation_requested.connect(self._start_simulation)
        self.settings_page.simulation_stopped.connect(self._stop_simulation)

        self.live_page.overlay_button.toggled.connect(self.overlay_button.setChecked)
        self.live_page.gaming_button.toggled.connect(self.gaming_button.setChecked)

    def _build_shortcuts(self) -> None:
        for index, (_label, key) in enumerate(PAGES, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))

        QShortcut(QKeySequence("Ctrl+M"), self).activated.connect(self.toggle_monitoring)
        QShortcut(QKeySequence("Ctrl+P"), self).activated.connect(
            lambda: self.pause_button.toggle()
        )
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(
            lambda: self.overlay_button.toggle()
        )
        QShortcut(QKeySequence("Ctrl+G"), self).activated.connect(
            lambda: self.gaming_button.toggle()
        )
        QShortcut(QKeySequence("F5"), self).activated.connect(self._refresh_active_page)
        QShortcut(QKeySequence("F1"), self).activated.connect(self.show_about)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()
        show_action = QAction("Show window", self)
        show_action.triggered.connect(self._restore_window)
        menu.addAction(show_action)

        self.tray_toggle_action = QAction("Start monitoring", self)
        self.tray_toggle_action.triggered.connect(self.toggle_monitoring)
        menu.addAction(self.tray_toggle_action)
        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._restore_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ navigation --
    def show_page(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.sidebar.buttons[key].setChecked(True)
        label = next(name for name, k in PAGES if k == key)
        self.page_title.setText(label)
        self._refresh_active_page()

    # ------------------------------------------------------------ monitoring --
    def toggle_monitoring(self) -> None:
        if self.service.running:
            self.service.stop()
        else:
            targets = self.service.repository.list_targets(enabled_only=True)
            if not targets:
                QMessageBox.information(
                    self, "No targets",
                    "There are no enabled targets to monitor.\n"
                    "Add one on the Targets page first.",
                )
                self.show_page("targets")
                return
            self.service.start()
        self._update_controls()

    def toggle_pause(self, paused: bool) -> None:
        if paused:
            self.service.pause()
        else:
            self.service.resume()
        self._update_controls()

    def set_overlay_visible(self, visible: bool) -> None:
        if visible:
            if self.overlay is None:
                self.overlay = OverlayWindow(self.settings.gaming)
            self.overlay.apply_settings(self.settings.gaming)
            self.overlay.show()
        elif self.overlay is not None:
            self.settings.gaming.overlay_x = self.overlay.x()
            self.settings.gaming.overlay_y = self.overlay.y()
            self.overlay.hide()
        self.settings.gaming.show_overlay = visible
        if self.live_page.overlay_button.isChecked() != visible:
            self.live_page.overlay_button.setChecked(visible)

    def set_gaming_mode(self, enabled: bool) -> None:
        """Plan section 46: lighter UI, monitoring untouched."""
        self.settings.gaming.enabled = enabled
        self.service.apply_settings(self.settings)
        self.refresh_timer.setInterval(self.settings.ui_refresh_ms())
        if self.live_page.gaming_button.isChecked() != enabled:
            self.live_page.gaming_button.setChecked(enabled)
        if enabled:
            self.show_page("live")
            self.status_message.setText(
                "Gaming mode: reduced UI updates, heavy diagnostics disabled."
            )
        else:
            self.status_message.setText("Gaming mode off.")
        self.settings.save()

    def _start_simulation(self, scenario: str) -> None:
        self.service.start_simulation(scenario)
        self.refresh_targets()
        self.show_page("overview")
        self._update_controls()

    def _stop_simulation(self) -> None:
        self.service.stop_simulation()
        self.refresh_targets()
        self._update_controls()

    # --------------------------------------------------------------- events --
    def _on_event(self, event: Event) -> None:
        self.overview_page.add_event(event)
        self.status_message.setText(event.message)

    def _on_incident(self, incident: Incident) -> None:
        if self.tray is not None and incident.severity.rank >= 3:
            self.tray.setToolTip(
                f"{APP_NAME} - {incident.severity.label} incident on {incident.target_name}"
            )

    def _on_notification(self, title: str, body: str, severity: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(
                title, body, QSystemTrayIcon.MessageIcon.Warning, 6000
            )
        else:
            self.status_message.setText(f"{title}: {body.splitlines()[0]}")

    def _on_state_changed(self, state: str) -> None:
        self._update_controls()
        self.status_message.setText(f"Monitoring {state}.")

    def _on_settings_applied(self) -> None:
        self.settings = self.service.settings
        self.refresh_timer.setInterval(self.settings.ui_refresh_ms())
        if self.overlay is not None:
            self.overlay.apply_settings(self.settings.gaming)
        self.status_message.setText("Settings applied.")

    def _show_incident(self, incident: Incident) -> None:
        IncidentDialog(incident, self).exec()

    # -------------------------------------------------------------- refresh --
    def refresh_targets(self) -> None:
        self.overview_page.refresh_targets()
        self.live_page.refresh_targets()
        self.targets_page.reload()

    def _refresh_active_page(self) -> None:
        current = self.stack.currentWidget()
        try:
            if current is self.overview_page:
                self.overview_page.refresh()
            elif current is self.live_page:
                self.live_page.refresh()
            elif current is self.diagnostics_page:
                if not (self.settings.gaming.enabled
                        and self.settings.gaming.disable_heavy_diagnostics):
                    self.diagnostics_page.refresh()
        except Exception as exc:  # pragma: no cover - one page must not kill the UI
            log.warning("Page refresh failed: %s", exc, exc_info=True)

        if self.overlay is not None and self.overlay.isVisible():
            self.overlay.update_metrics(
                self.service.primary_stats(), self.service.spike_count()
            )
        self._update_status_bar()

    def _update_status_bar(self) -> None:
        service = self.service
        if not service.running:
            self.status_detail.setText("")
            self.connectivity_label.setText("")
            return
        stats = service.stats()
        probes = sum(s.sample_count for s in stats.values())
        self.status_detail.setText(
            f"{len(stats)} targets · {probes} probes · {format_duration(service.uptime_s())}"
        )
        connectivity = service.connectivity_state()
        self.connectivity_label.setText(connectivity or "")

    def _update_controls(self) -> None:
        service = self.service
        running = service.running
        self.start_button.setText("Stop monitoring" if running else "Start monitoring")
        self.pause_button.setEnabled(running and not service.simulating)
        if not running:
            self.pause_button.blockSignals(True)
            self.pause_button.setChecked(False)
            self.pause_button.blockSignals(False)

        status = service.monitor.overall_status() if running else NodeStatus.UNKNOWN
        self.sidebar.set_state(running, service.paused, service.simulating, status)
        if self.tray is not None:
            self.tray_toggle_action.setText(
                "Stop monitoring" if running else "Start monitoring"
            )

    # --------------------------------------------------------------- window --
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().resizeEvent(event)
        compact = self.width() < COMPACT_SIDEBAR_WIDTH
        if compact != self._compact:
            self._compact = compact
            self.sidebar.set_compact(compact)

    def show_about(self) -> None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(f"About {APP_NAME}")
        logo = logo_pixmap(96)
        if logo is not None:
            dialog.setIconPixmap(logo)
        dialog.setText(f"<b>{APP_NAME}</b> v{__version__}")
        dialog.setInformativeText(
            "Monitor. Analyze. Protect. Play.<br><br>"
            "Continuous multi-layer network monitoring that compares your router, "
            "the public internet and your game server so a lag spike can be "
            "attributed to a layer instead of guessed at.<br><br>"
            "Local-first: no telemetry, no account, no data leaves this machine.<br><br>"
            "<b>Shortcuts</b><br>"
            "Ctrl+1..6 pages · Ctrl+M start/stop · Ctrl+P pause<br>"
            "Ctrl+O overlay · Ctrl+G gaming mode · F5 refresh · F1 about"
        )
        dialog.exec()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        log.info("Shutting down")
        self.refresh_timer.stop()
        if self.overlay is not None:
            self.settings.gaming.overlay_x = self.overlay.x()
            self.settings.gaming.overlay_y = self.overlay.y()
            self.overlay.close()
        self.settings.save()
        self.service.shutdown()
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)


def apply_theme(app: QApplication, scale: float = 1.0) -> None:
    app.setStyleSheet(stylesheet(PALETTE, scale))
