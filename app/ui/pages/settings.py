"""Settings page (plan sections 56, 40, 43, 62, 63, 65).

Changes are applied to the live monitor and persisted as soon as they are
saved; nothing here requires a restart except the theme scale.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import (
    MIN_INTERVAL_MS,
    RETENTION_CHOICES,
    IPVersion,
    Severity,
)
from app.config.settings import Settings
from app.core.simulator import SCENARIOS, SCENARIO_DESCRIPTIONS, SCENARIO_LABELS
from app.utils import autostart
from app.services.monitoring_service import MonitoringService
from app.ui.theme import SPACING
from app.utils.logger import LEVELS, log_file_path, set_level
from app.utils.platform import open_in_file_manager
from app.utils.time import format_bytes

RESTART_NOTE = "Interface scale takes effect after a restart."


class SettingsPage(QWidget):
    """Editor for every persisted setting."""

    settings_applied = Signal()
    simulation_requested = Signal(str)
    simulation_stopped = Signal()

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service

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

        title = QLabel("SETTINGS")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        layout.addWidget(self._build_monitoring_group())
        layout.addWidget(self._build_detection_group())
        layout.addWidget(self._build_appearance_group())
        layout.addWidget(self._build_notifications_group())
        layout.addWidget(self._build_storage_group())
        layout.addWidget(self._build_advanced_group())
        layout.addWidget(self._build_simulation_group())

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save settings")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self.apply)
        self.reset_button = QPushButton("Restore defaults")
        self.reset_button.clicked.connect(self._restore_defaults)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        self.saved_label = QLabel("")
        self.saved_label.setObjectName("Faint")
        buttons.addWidget(self.saved_label)
        layout.addLayout(buttons)
        layout.addStretch(1)

        self.load(self.service.settings)

    # -------------------------------------------------------------- groups --
    def _build_monitoring_group(self) -> QGroupBox:
        group = QGroupBox("Monitoring")
        form = QFormLayout(group)
        form.setSpacing(9)

        self.gateway_interval = self._interval_spin()
        self.internet_interval = self._interval_spin()
        self.custom_interval = self._interval_spin()
        self.timeout = QSpinBox()
        self.timeout.setRange(100, 10000)
        self.timeout.setSingleStep(100)
        self.timeout.setSuffix(" ms")

        self.ip_version = QComboBox()
        for version in IPVersion:
            self.ip_version.addItem(version.label, version.value)

        self.auto_start = QCheckBox("Start monitoring automatically at launch")
        self.system_sampling = QCheckBox("Sample throughput and system usage")
        self.start_with_system = QCheckBox("Start Internet Overwatch when I sign in")
        self.start_with_system.setEnabled(autostart.supported())
        self.start_minimised = QCheckBox("Start minimised to the system tray")

        form.addRow("Router interval", self.gateway_interval)
        form.addRow("Internet interval", self.internet_interval)
        form.addRow("Game / custom interval", self.custom_interval)
        form.addRow("Probe timeout", self.timeout)
        form.addRow("IP version", self.ip_version)
        form.addRow("", self.auto_start)
        form.addRow("", self.system_sampling)
        form.addRow("", self.start_with_system)
        form.addRow("", self.start_minimised)

        note = QLabel(
            f"Minimum interval is {MIN_INTERVAL_MS} ms per target, to avoid flooding."
        )
        note.setObjectName("Faint")
        form.addRow("", note)
        return group

    @staticmethod
    def _interval_spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(MIN_INTERVAL_MS, 60000)
        spin.setSingleStep(50)
        spin.setSuffix(" ms")
        return spin

    def _build_detection_group(self) -> QGroupBox:
        group = QGroupBox("Detection sensitivity")
        form = QFormLayout(group)
        form.setSpacing(9)

        self.spike_absolute = QDoubleSpinBox()
        self.spike_absolute.setRange(5, 2000)
        self.spike_absolute.setSuffix(" ms")
        self.spike_absolute.setToolTip(
            "A sample must exceed the baseline by at least this much to count as a spike"
        )

        self.spike_multiplier = QDoubleSpinBox()
        self.spike_multiplier.setRange(1.05, 20.0)
        self.spike_multiplier.setSingleStep(0.1)
        self.spike_multiplier.setSuffix(" x baseline")

        self.rolling_window = QSpinBox()
        self.rolling_window.setRange(8, 600)
        self.rolling_window.setSuffix(" samples")
        self.rolling_window.setToolTip("Window used for the rolling median baseline")

        self.incident_gap = QDoubleSpinBox()
        self.incident_gap.setRange(0.5, 60.0)
        self.incident_gap.setSingleStep(0.5)
        self.incident_gap.setSuffix(" s")
        self.incident_gap.setToolTip(
            "Spikes closer together than this are merged into one incident"
        )

        self.loss_window = QSpinBox()
        self.loss_window.setRange(5, 1000)
        self.loss_window.setSuffix(" samples")

        form.addRow("Spike threshold", self.spike_absolute)
        form.addRow("Spike multiplier", self.spike_multiplier)
        form.addRow("Baseline window", self.rolling_window)
        form.addRow("Incident merge gap", self.incident_gap)
        form.addRow("Loss window", self.loss_window)

        severity = QHBoxLayout()
        self.severity_minor = self._severity_spin()
        self.severity_moderate = self._severity_spin()
        self.severity_severe = self._severity_spin()
        self.severity_critical = self._severity_spin()
        for label, spin in (
            ("Minor", self.severity_minor),
            ("Moderate", self.severity_moderate),
            ("Severe", self.severity_severe),
            ("Critical", self.severity_critical),
        ):
            block = QVBoxLayout()
            caption = QLabel(label)
            caption.setObjectName("Faint")
            block.addWidget(caption)
            block.addWidget(spin)
            severity.addLayout(block)
        severity.addStretch(1)
        form.addRow("Severity bands", severity)
        return group

    @staticmethod
    def _severity_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(1, 10000)
        spin.setSuffix(" ms")
        spin.setMaximumWidth(110)
        return spin

    def _build_appearance_group(self) -> QGroupBox:
        group = QGroupBox("Appearance")
        form = QFormLayout(group)

        self.theme = QComboBox()
        self.theme.addItem("Dark", "dark")

        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.75, 2.0)
        self.scale.setSingleStep(0.05)
        self.scale.setToolTip(RESTART_NOTE)

        self.compact_mode = QCheckBox("Compact layout")
        self.show_millis = QCheckBox("Show milliseconds in the event feed")

        form.addRow("Theme", self.theme)
        form.addRow("Interface scale", self.scale)
        form.addRow("", self.compact_mode)
        form.addRow("", self.show_millis)

        note = QLabel(RESTART_NOTE)
        note.setObjectName("Faint")
        form.addRow("", note)
        return group

    def _build_notifications_group(self) -> QGroupBox:
        group = QGroupBox("Notifications")
        form = QFormLayout(group)

        self.notifications_enabled = QCheckBox("Show desktop notifications")
        self.min_severity = QComboBox()
        for severity in Severity:
            self.min_severity.addItem(severity.label, severity.value)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setSuffix(" s")
        self.cooldown.setToolTip("Minimum time between notifications for the same target")
        self.sound_enabled = QCheckBox("Play a sound (off by default)")

        form.addRow("", self.notifications_enabled)
        form.addRow("Minimum severity", self.min_severity)
        form.addRow("Cooldown", self.cooldown)
        form.addRow("", self.sound_enabled)
        return group

    def _build_storage_group(self) -> QGroupBox:
        group = QGroupBox("Storage")
        form = QFormLayout(group)

        self.retention = QComboBox()
        for label, days in RETENTION_CHOICES:
            self.retention.addItem(label, days)

        self.flush_seconds = QDoubleSpinBox()
        self.flush_seconds.setRange(0.5, 60.0)
        self.flush_seconds.setSingleStep(0.5)
        self.flush_seconds.setSuffix(" s")
        self.flush_seconds.setToolTip("How often batched samples are written to disk")

        self.db_label = QLabel("")
        self.db_label.setObjectName("Faint")
        self.db_label.setWordWrap(True)

        buttons = QHBoxLayout()
        self.export_db_button = QPushButton("Export database")
        self.export_db_button.clicked.connect(self._export_database)
        self.clear_history_button = QPushButton("Delete history")
        self.clear_history_button.clicked.connect(self._clear_history)
        self.clear_db_button = QPushButton("Clear database")
        self.clear_db_button.setObjectName("Danger")
        self.clear_db_button.clicked.connect(self._clear_database)
        for button in (self.export_db_button, self.clear_history_button,
                       self.clear_db_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        form.addRow("Retention", self.retention)
        form.addRow("Write interval", self.flush_seconds)
        form.addRow("Database", self.db_label)
        form.addRow("", buttons)
        return group

    def _build_advanced_group(self) -> QGroupBox:
        group = QGroupBox("Advanced")
        form = QFormLayout(group)

        self.ping_impl = QComboBox()
        for label, value in (
            ("Automatic", "auto"),
            ("Raw socket", "socket"),
            ("System ping binary", "system"),
        ):
            self.ping_impl.addItem(label, value)

        from PySide6.QtWidgets import QLineEdit

        self.traceroute_command = QLineEdit()
        self.traceroute_command.setPlaceholderText(
            "Leave empty for the platform default (tracert / traceroute)"
        )
        self.dns_resolvers = QLineEdit()
        self.dns_resolvers.setPlaceholderText("1.1.1.1, 8.8.8.8")
        self.dns_probe_host = QLineEdit()

        self.debug_logging = QCheckBox("Debug logging")
        self.debug_logging.toggled.connect(
            lambda checked: set_level("DEBUG" if checked else "INFO")
        )

        self.log_button = QPushButton("Open logs")
        self.log_button.clicked.connect(self._open_logs)

        form.addRow("Ping implementation", self.ping_impl)
        form.addRow("Traceroute command", self.traceroute_command)
        form.addRow("DNS resolvers", self.dns_resolvers)
        form.addRow("DNS probe host", self.dns_probe_host)
        form.addRow("", self.debug_logging)
        form.addRow("", self.log_button)

        privacy = QLabel(
            "Internet Overwatch is local-first: no telemetry, no account, and traffic "
            "only to the targets you configure."
        )
        privacy.setWordWrap(True)
        privacy.setObjectName("Faint")
        form.addRow("", privacy)
        return group

    def _build_simulation_group(self) -> QGroupBox:
        group = QGroupBox("Synthetic test mode")
        form = QFormLayout(group)

        self.scenario_combo = QComboBox()
        for scenario in SCENARIOS:
            self.scenario_combo.addItem(SCENARIO_LABELS[scenario], scenario)
        self.scenario_combo.currentIndexChanged.connect(self._on_scenario_changed)

        self.scenario_note = QLabel("")
        self.scenario_note.setWordWrap(True)
        self.scenario_note.setObjectName("Faint")

        buttons = QHBoxLayout()
        self.simulate_button = QPushButton("Start simulation")
        self.simulate_button.clicked.connect(self._start_simulation)
        self.stop_simulate_button = QPushButton("Stop simulation")
        self.stop_simulate_button.clicked.connect(self.simulation_stopped)
        buttons.addWidget(self.simulate_button)
        buttons.addWidget(self.stop_simulate_button)
        buttons.addStretch(1)

        form.addRow("Scenario", self.scenario_combo)
        form.addRow("", self.scenario_note)
        form.addRow("", buttons)

        warning = QLabel(
            "Simulation replaces live monitoring with generated data so the dashboard "
            "and detection rules can be exercised without a real network fault."
        )
        warning.setWordWrap(True)
        warning.setObjectName("Faint")
        form.addRow("", warning)
        self._on_scenario_changed()
        return group

    def _on_scenario_changed(self) -> None:
        scenario = self.scenario_combo.currentData()
        self.scenario_note.setText(SCENARIO_DESCRIPTIONS.get(scenario, ""))

    def _start_simulation(self) -> None:
        self.simulation_requested.emit(self.scenario_combo.currentData())

    # ------------------------------------------------------------- load/save --
    def load(self, settings: Settings) -> None:
        monitoring = settings.monitoring
        self.gateway_interval.setValue(monitoring.gateway_interval_ms)
        self.internet_interval.setValue(monitoring.internet_interval_ms)
        self.custom_interval.setValue(monitoring.custom_interval_ms)
        self.timeout.setValue(monitoring.timeout_ms)
        self.ip_version.setCurrentIndex(max(0, self.ip_version.findData(monitoring.ip_version)))
        self.auto_start.setChecked(monitoring.auto_start_monitoring)
        self.system_sampling.setChecked(monitoring.system_sampling_enabled)
        # Read the real OS state rather than trusting the stored flag, which can
        # drift if the entry is removed outside the app.
        self.start_with_system.setChecked(autostart.is_enabled())
        self.start_minimised.setChecked(monitoring.start_minimised)

        detection = settings.detection
        self.spike_absolute.setValue(detection.spike_absolute_ms)
        self.spike_multiplier.setValue(detection.spike_multiplier)
        self.rolling_window.setValue(detection.rolling_window)
        self.incident_gap.setValue(detection.incident_gap_seconds)
        self.loss_window.setValue(detection.loss_window_samples)
        self.severity_minor.setValue(detection.severity_minor_ms)
        self.severity_moderate.setValue(detection.severity_moderate_ms)
        self.severity_severe.setValue(detection.severity_severe_ms)
        self.severity_critical.setValue(detection.severity_critical_ms)

        appearance = settings.appearance
        self.scale.setValue(appearance.scale)
        self.compact_mode.setChecked(appearance.compact_mode)
        self.show_millis.setChecked(appearance.show_millis_in_events)

        notifications = settings.notifications
        self.notifications_enabled.setChecked(notifications.enabled)
        self.min_severity.setCurrentIndex(
            max(0, self.min_severity.findData(notifications.minimum_severity))
        )
        self.cooldown.setValue(notifications.cooldown_seconds)
        self.sound_enabled.setChecked(notifications.sound_enabled)

        storage = settings.storage
        index = self.retention.findData(storage.retention_days)
        self.retention.setCurrentIndex(index if index >= 0 else 1)
        self.flush_seconds.setValue(storage.flush_seconds)
        self._update_db_label()

        advanced = settings.advanced
        self.ping_impl.setCurrentIndex(
            max(0, self.ping_impl.findData(advanced.ping_implementation))
        )
        self.traceroute_command.setText(advanced.traceroute_command)
        self.dns_resolvers.setText(", ".join(advanced.dns_resolvers))
        self.dns_probe_host.setText(advanced.dns_probe_host)
        self.debug_logging.setChecked(advanced.debug_logging)

    def collect(self) -> Settings:
        settings = self.service.settings

        settings.monitoring.gateway_interval_ms = self.gateway_interval.value()
        settings.monitoring.internet_interval_ms = self.internet_interval.value()
        settings.monitoring.custom_interval_ms = self.custom_interval.value()
        settings.monitoring.timeout_ms = self.timeout.value()
        settings.monitoring.ip_version = self.ip_version.currentData()
        settings.monitoring.auto_start_monitoring = self.auto_start.isChecked()
        settings.monitoring.system_sampling_enabled = self.system_sampling.isChecked()
        settings.monitoring.start_minimised = self.start_minimised.isChecked()

        wanted = self.start_with_system.isChecked()
        if wanted != autostart.is_enabled():
            if autostart.set_enabled(wanted):
                settings.monitoring.start_with_system = wanted
            else:
                self.start_with_system.setChecked(autostart.is_enabled())
                self.saved_label.setText("Could not change the sign-in setting.")

        settings.detection.spike_absolute_ms = self.spike_absolute.value()
        settings.detection.spike_multiplier = self.spike_multiplier.value()
        settings.detection.rolling_window = self.rolling_window.value()
        settings.detection.incident_gap_seconds = self.incident_gap.value()
        settings.detection.loss_window_samples = self.loss_window.value()
        settings.detection.severity_minor_ms = self.severity_minor.value()
        settings.detection.severity_moderate_ms = self.severity_moderate.value()
        settings.detection.severity_severe_ms = self.severity_severe.value()
        settings.detection.severity_critical_ms = self.severity_critical.value()

        settings.appearance.scale = self.scale.value()
        settings.appearance.compact_mode = self.compact_mode.isChecked()
        settings.appearance.show_millis_in_events = self.show_millis.isChecked()

        settings.notifications.enabled = self.notifications_enabled.isChecked()
        settings.notifications.minimum_severity = self.min_severity.currentData()
        settings.notifications.cooldown_seconds = self.cooldown.value()
        settings.notifications.sound_enabled = self.sound_enabled.isChecked()

        settings.storage.retention_days = int(self.retention.currentData())
        settings.storage.flush_seconds = self.flush_seconds.value()

        settings.advanced.ping_implementation = self.ping_impl.currentData()
        settings.advanced.traceroute_command = self.traceroute_command.text().strip()
        settings.advanced.dns_resolvers = [
            r.strip() for r in self.dns_resolvers.text().split(",") if r.strip()
        ]
        settings.advanced.dns_probe_host = self.dns_probe_host.text().strip() or "example.com"
        settings.advanced.debug_logging = self.debug_logging.isChecked()

        settings.validate()
        return settings

    def apply(self) -> None:
        settings = self.collect()
        settings.save()
        self.service.apply_settings(settings)
        set_level(settings.advanced.log_level)
        self.saved_label.setText("Settings saved.")
        self.settings_applied.emit()

    def _restore_defaults(self) -> None:
        confirm = QMessageBox.question(
            self, "Restore defaults", "Reset every setting to its default value?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        from app.config.settings import reset_settings

        settings = reset_settings()
        self.service.apply_settings(settings)
        self.load(settings)
        self.saved_label.setText("Defaults restored.")
        self.settings_applied.emit()

    # -------------------------------------------------------------- storage --
    def _update_db_label(self) -> None:
        stats = self.service.repository.statistics()
        path = self.service.repository.db.path
        self.db_label.setText(
            f"{path}\n{stats['samples']} samples, {stats['events']} events, "
            f"{stats['sessions']} sessions · {format_bytes(stats['size_bytes'])}"
        )

    def _export_database(self) -> None:
        from app.services.export_service import export_database, suggested_filename

        path, _ = QFileDialog.getSaveFileName(
            self, "Export database", suggested_filename("overwatch", "db"),
            "SQLite database (*.db);;All files (*)",
        )
        if not path:
            return
        if export_database(self.service.repository, path):
            QMessageBox.information(self, "Export", f"Database exported to:\n{path}")
        else:
            QMessageBox.warning(self, "Export", "Could not export the database.")

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(
            self, "Delete history",
            "Delete all sessions, samples and events?\nTargets and settings are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self.service.running:
            self.service.stop()
        self.service.repository.clear_history()
        self._update_db_label()
        QMessageBox.information(self, "History", "History deleted.")

    def _clear_database(self) -> None:
        confirm = QMessageBox.question(
            self, "Clear database",
            "Delete everything, including your configured targets?\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self.service.running:
            self.service.stop()
        self.service.repository.clear_database()
        self.service.ensure_targets()
        self.service.reload_targets()
        self._update_db_label()
        QMessageBox.information(self, "Database", "Database cleared and defaults restored.")

    def _open_logs(self) -> None:
        path = log_file_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        if not open_in_file_manager(path):
            QMessageBox.information(self, "Logs", f"Log file:\n{path}")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        self._update_db_label()
        self.saved_label.setText("")
