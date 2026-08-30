"""Targets page - manage what is monitored (plan sections 27, 28)."""

from __future__ import annotations

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
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import MIN_INTERVAL_MS, Protocol, TargetCategory
from app.network.gateway import detect_gateway
from app.services.monitoring_service import MonitoringService
from app.storage.models import Target
from app.ui.theme import PALETTE, SPACING
from app.ui.widgets.status_card import Card
from app.utils.logger import get_logger

log = get_logger("ui.targets")


class TargetDialog(QDialog):
    """Add or edit a single target."""

    def __init__(self, target: Target | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit target" if target else "Add target")
        self.setMinimumWidth(420)
        self.target = target or Target(name="", host="", interval_ms=500)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.name_input = QLineEdit(self.target.name)
        self.name_input.setPlaceholderText("Display name, e.g. Game Server")
        form.addRow("Name", self.name_input)

        self.host_input = QLineEdit(self.target.host)
        self.host_input.setPlaceholderText("IP address or hostname")
        form.addRow("Host", self.host_input)

        self.protocol_combo = QComboBox()
        for protocol in Protocol:
            self.protocol_combo.addItem(protocol.label, protocol.value)
        index = self.protocol_combo.findData(self.target.protocol)
        self.protocol_combo.setCurrentIndex(max(0, index))
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        form.addRow("Protocol", self.protocol_combo)

        self.port_input = QSpinBox()
        self.port_input.setRange(0, 65535)
        self.port_input.setSpecialValueText("none")
        self.port_input.setValue(self.target.port or 0)
        form.addRow("Port", self.port_input)

        self.category_combo = QComboBox()
        for category in TargetCategory:
            self.category_combo.addItem(category.label, category.value)
        index = self.category_combo.findData(self.target.category)
        self.category_combo.setCurrentIndex(max(0, index))
        form.addRow("Category", self.category_combo)

        self.interval_input = QSpinBox()
        self.interval_input.setRange(MIN_INTERVAL_MS, 60000)
        self.interval_input.setSingleStep(50)
        self.interval_input.setSuffix(" ms")
        self.interval_input.setValue(max(MIN_INTERVAL_MS, self.target.interval_ms))
        form.addRow("Interval", self.interval_input)

        self.enabled_check = QCheckBox("Monitor this target")
        self.enabled_check.setChecked(self.target.enabled)
        form.addRow("", self.enabled_check)

        layout.addLayout(form)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setObjectName("Faint")
        layout.addWidget(self.note)
        self._on_protocol_changed()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_protocol_changed(self) -> None:
        protocol = Protocol(self.protocol_combo.currentData())
        self.note.setText(protocol.measurement_note)
        needs_port = protocol == Protocol.TCP
        self.port_input.setEnabled(needs_port)
        if needs_port and not self.port_input.value():
            self.port_input.setValue(443)
        if protocol == Protocol.DNS and not self.host_input.text():
            self.host_input.setPlaceholderText("Resolver IP, e.g. 1.1.1.1")

    def _accept(self) -> None:
        name = self.name_input.text().strip()
        host = self.host_input.text().strip()
        if not name or not host:
            QMessageBox.warning(self, "Target", "A name and a host are required.")
            return
        protocol = Protocol(self.protocol_combo.currentData())
        if protocol == Protocol.TCP and not self.port_input.value():
            QMessageBox.warning(self, "Target", "TCP targets need a port.")
            return

        self.target.name = name
        self.target.host = host
        self.target.protocol = protocol.value
        self.target.port = self.port_input.value() or None
        self.target.category = self.category_combo.currentData()
        self.target.interval_ms = self.interval_input.value()
        self.target.enabled = self.enabled_check.isChecked()
        self.accept()


class TargetsPage(QWidget):
    """Table of monitored targets with add/edit/remove/enable."""

    targets_changed = Signal()

    COLUMNS = ["", "Name", "Host", "Protocol", "Category", "Interval", "Measures"]

    def __init__(self, service: MonitoringService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.repository = service.repository

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING + 4, SPACING + 4, SPACING + 4, SPACING + 4)
        layout.setSpacing(SPACING)

        header = QHBoxLayout()
        title = QLabel("TARGETS")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.add_button = QPushButton("Add target")
        self.add_button.setObjectName("Primary")
        self.add_button.clicked.connect(self._add_target)
        header.addWidget(self.add_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit_target)
        header.addWidget(self.edit_button)

        self.toggle_button = QPushButton("Enable / disable")
        self.toggle_button.clicked.connect(self._toggle_target)
        header.addWidget(self.toggle_button)

        self.delete_button = QPushButton("Remove")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_target)
        header.addWidget(self.delete_button)

        self.detect_button = QPushButton("Detect gateway")
        self.detect_button.setToolTip("Re-detect the router address from the routing table")
        self.detect_button.clicked.connect(self._detect_gateway)
        header.addWidget(self.detect_button)
        layout.addLayout(header)

        card = Card("Monitored targets")
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 26)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 200)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_target())
        card.add(self.table, 1)
        layout.addWidget(card, 1)

        note = QLabel(
            "Monitor at least one gateway target, two public targets and your game "
            "server. Comparing them is what makes a diagnosis possible. "
            "Intervals below 100 ms are not allowed, to avoid flooding a target."
        )
        note.setWordWrap(True)
        note.setObjectName("Faint")
        layout.addWidget(note)

    # -------------------------------------------------------------- loading --
    def reload(self) -> None:
        targets = self.repository.list_targets()
        self.table.setRowCount(len(targets))
        for row, target in enumerate(targets):
            dot = QTableWidgetItem("●" if target.enabled else "○")
            dot.setForeground(
                self.palette().text() if not target.enabled else
                self._color(target.color)
            )
            dot.setToolTip("Enabled" if target.enabled else "Disabled")
            dot.setData(Qt.ItemDataRole.UserRole, target.id)
            self.table.setItem(row, 0, dot)

            values = [
                target.name,
                target.display_host,
                target.protocol_enum.label,
                target.category_enum.label,
                f"{target.interval_ms} ms",
                target.measurement_label,
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, target.id)
                if not target.enabled:
                    item.setForeground(self._color(PALETTE.text_faint))
                self.table.setItem(row, column, item)

    @staticmethod
    def _color(value: str):
        from PySide6.QtGui import QColor

        return QColor(value)

    def selected_target(self) -> Target | None:
        items = self.table.selectedItems()
        if not items:
            return None
        target_id = items[0].data(Qt.ItemDataRole.UserRole)
        if target_id is None:
            return None
        return self.repository.get_target(int(target_id))

    # -------------------------------------------------------------- actions --
    def _add_target(self) -> None:
        dialog = TargetDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.add_target(dialog.target)
            self._after_change()

    def _edit_target(self) -> None:
        target = self.selected_target()
        if target is None:
            QMessageBox.information(self, "Targets", "Select a target first.")
            return
        dialog = TargetDialog(target, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.update_target(dialog.target)
            self._after_change()

    def _toggle_target(self) -> None:
        target = self.selected_target()
        if target is None:
            return
        self.repository.set_target_enabled(target.id, not target.enabled)
        self._after_change()

    def _delete_target(self) -> None:
        target = self.selected_target()
        if target is None:
            return
        confirm = QMessageBox.question(
            self, "Remove target",
            f"Remove '{target.name}'?\nStored samples for it are kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.repository.delete_target(target.id)
        self._after_change()

    def _detect_gateway(self) -> None:
        gateway = detect_gateway()
        if not gateway.found:
            QMessageBox.warning(
                self, "Gateway",
                "No default gateway could be detected from the routing table.",
            )
            return

        existing = [t for t in self.repository.list_targets()
                    if t.category == TargetCategory.GATEWAY.value]
        if existing:
            for target in existing:
                target.host = gateway.address
                self.repository.update_target(target)
            message = f"Gateway targets updated to {gateway.address}"
        else:
            self.repository.add_target(
                Target(
                    name="Router",
                    host=gateway.address,
                    protocol=Protocol.ICMP.value,
                    interval_ms=self.service.settings.monitoring.gateway_interval_ms,
                    enabled=True,
                    category=TargetCategory.GATEWAY.value,
                )
            )
            message = f"Router target added for {gateway.address}"
        self._after_change()
        QMessageBox.information(
            self, "Gateway",
            f"{message}\nDetected via {gateway.source}"
            + (f" on {gateway.interface}" if gateway.interface else ""),
        )

    def _after_change(self) -> None:
        self.reload()
        self.service.reload_targets()
        self.targets_changed.emit()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        self.reload()
