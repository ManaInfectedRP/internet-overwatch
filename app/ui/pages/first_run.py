"""First-run setup (plan section 57).

Detects the adapter, gateway and DNS servers, then asks what to monitor. It
never invents a gateway address - if detection fails, the user is told and the
local option is disabled rather than silently defaulting to 192.168.1.1.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import (
    APP_NAME,
    DEFAULT_GAME_HOST,
    DEFAULT_GAME_NAME,
    DEFAULT_GAME_PORT,
    DEFAULT_GAME_REALM_HOST,
    DEFAULT_GAME_REALM_NAME,
    DEFAULT_GAME_REALM_PORT,
    Protocol,
    TargetCategory,
)
from app.network.gateway import GatewayInfo, detect_gateway
from app.network.interfaces import NetworkInfo, collect_network_info
from app.storage.models import Target
from app.ui.theme import PALETTE, SPACING
from app.utils.assets import logo_pixmap
from app.utils.logger import get_logger

log = get_logger("ui.first_run")


class FirstRunDialog(QDialog):
    """Welcome + connection detection + target selection."""

    detection_finished = Signal(object, object)  # GatewayInfo, NetworkInfo

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setMinimumWidth(560)
        self._gateway: GatewayInfo | None = None
        self._network: NetworkInfo | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING)

        header = QHBoxLayout()
        logo = logo_pixmap(72)
        if logo is not None:
            mark = QLabel()
            mark.setPixmap(logo)
            header.addWidget(mark)

        heading = QVBoxLayout()
        title = QLabel(f"Welcome to {APP_NAME}")
        title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        subtitle = QLabel("Let's configure your connection.")
        subtitle.setObjectName("Muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        heading.addStretch(1)
        header.addLayout(heading, 1)
        layout.addLayout(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate while detecting
        layout.addWidget(self.progress)

        self.detection_label = QLabel("Detecting your network...")
        self.detection_label.setWordWrap(True)
        self.detection_label.setObjectName("Muted")
        layout.addWidget(self.detection_label)

        form = QFormLayout()
        form.setSpacing(9)

        self.local_check = QCheckBox("Local network (your router)")
        self.local_check.setChecked(True)
        self.local_check.setEnabled(False)
        self.internet_check = QCheckBox("Internet (Cloudflare and Google DNS)")
        self.internet_check.setChecked(True)
        self.custom_check = QCheckBox("A custom or game server")
        self.custom_check.setChecked(True)
        self.custom_check.toggled.connect(self._on_custom_toggled)

        form.addRow("Monitor", self.local_check)
        form.addRow("", self.internet_check)
        form.addRow("", self.custom_check)

        self.custom_name = QLineEdit(DEFAULT_GAME_NAME)
        self.custom_host = QLineEdit(DEFAULT_GAME_HOST)
        self.custom_host.setPlaceholderText("hostname or IP of your game server")
        self.custom_port = QSpinBox()
        self.custom_port.setRange(0, 65535)
        self.custom_port.setSpecialValueText("none (ICMP)")
        self.custom_port.setValue(DEFAULT_GAME_PORT)
        self.custom_port.setToolTip(
            "Set a port to measure TCP connect time when the server does not answer ping"
        )
        self.protocol_combo = QComboBox()
        for protocol in (Protocol.ICMP, Protocol.TCP):
            self.protocol_combo.addItem(protocol.label, protocol.value)
        form.addRow("Name", self.custom_name)
        form.addRow("Host", self.custom_host)
        form.addRow("Port", self.custom_port)
        form.addRow("Measure with", self.protocol_combo)

        self.realm_check = QCheckBox(
            f"Also monitor {DEFAULT_GAME_REALM_NAME} ({DEFAULT_GAME_REALM_HOST})"
        )
        self.realm_check.setChecked(True)
        self.realm_check.setToolTip(
            "Monitoring the realm as well as the login host separates a login "
            "infrastructure problem from a bad route to the realm you play on"
        )
        form.addRow("", self.realm_check)
        layout.addLayout(form)

        note = QLabel(
            "A TCP connect measurement is not the same as in-game latency, but it "
            "still shows when the route to that server degrades. "
            "You can change all of this later on the Targets page."
        )
        note.setWordWrap(True)
        note.setObjectName("Faint")
        layout.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start monitoring")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.detection_finished.connect(self._on_detection_finished)
        self._start_detection()

    # ------------------------------------------------------------ detection --
    def _start_detection(self) -> None:
        def task() -> None:
            try:
                gateway = detect_gateway()
                network = collect_network_info()
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("First-run detection failed: %s", exc)
                gateway, network = GatewayInfo(), NetworkInfo()
            self.detection_finished.emit(gateway, network)

        threading.Thread(target=task, daemon=True, name="first-run-detect").start()

    def _on_detection_finished(self, gateway: GatewayInfo, network: NetworkInfo) -> None:
        self._gateway = gateway
        self._network = network
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setVisible(False)

        lines = []
        if network.default_interface is not None:
            iface = network.default_interface
            lines.append(
                f"Adapter: {iface.name} ({iface.connection_type}, {iface.link_speed_text})"
            )
            lines.append(f"Address: {iface.ipv4 or 'unknown'}")
        if gateway.found:
            lines.append(f"Gateway: {gateway.address}  (via {gateway.source})")
            self.local_check.setEnabled(True)
        else:
            lines.append("Gateway: not detected - local network monitoring is unavailable.")
            self.local_check.setChecked(False)
            self.local_check.setEnabled(False)
        if network.dns_servers:
            lines.append(f"DNS: {', '.join(network.dns_servers[:3])}")

        self.detection_label.setText("\n".join(lines))
        self.detection_label.setStyleSheet(
            f"color: {PALETTE.text if gateway.found else PALETTE.warning};"
        )

    def _on_custom_toggled(self, checked: bool) -> None:
        for widget in (self.custom_name, self.custom_host, self.custom_port,
                       self.protocol_combo, self.realm_check):
            widget.setEnabled(checked)
        if checked:
            self.custom_host.setFocus()

    # -------------------------------------------------------------- results --
    def selected_targets(self) -> list[Target]:
        """Targets chosen in the wizard, ready to be stored."""
        from app.config.defaults import (
            DEFAULT_GATEWAY_INTERVAL_MS,
            DEFAULT_INTERNET_INTERVAL_MS,
            DEFAULT_CUSTOM_INTERVAL_MS,
        )

        targets: list[Target] = []
        if self.local_check.isChecked() and self._gateway and self._gateway.found:
            targets.append(Target(
                name="Router",
                host=self._gateway.address,
                protocol=Protocol.ICMP.value,
                interval_ms=DEFAULT_GATEWAY_INTERVAL_MS,
                enabled=True,
                category=TargetCategory.GATEWAY.value,
            ))
        if self.internet_check.isChecked():
            for name, host in (("Cloudflare", "1.1.1.1"), ("Google DNS", "8.8.8.8")):
                targets.append(Target(
                    name=name,
                    host=host,
                    protocol=Protocol.ICMP.value,
                    interval_ms=DEFAULT_INTERNET_INTERVAL_MS,
                    enabled=True,
                    category=TargetCategory.INTERNET.value,
                ))
        if self.custom_check.isChecked() and self.custom_host.text().strip():
            port = self.custom_port.value() or None
            protocol = Protocol(self.protocol_combo.currentData())
            if protocol == Protocol.TCP and not port:
                # TCP needs a port; fall back to ICMP rather than storing a
                # target that can never produce a measurement.
                protocol = Protocol.ICMP
            targets.append(Target(
                name=self.custom_name.text().strip() or "Game Server",
                host=self.custom_host.text().strip(),
                port=port,
                protocol=protocol.value,
                interval_ms=DEFAULT_CUSTOM_INTERVAL_MS,
                enabled=True,
                category=TargetCategory.CUSTOM.value,
            ))
        if self.custom_check.isChecked() and self.realm_check.isChecked():
            targets.append(Target(
                name=DEFAULT_GAME_REALM_NAME,
                host=DEFAULT_GAME_REALM_HOST,
                port=DEFAULT_GAME_REALM_PORT,
                protocol=Protocol.ICMP.value,
                interval_ms=DEFAULT_CUSTOM_INTERVAL_MS,
                enabled=True,
                category=TargetCategory.CUSTOM.value,
            ))
        return targets
