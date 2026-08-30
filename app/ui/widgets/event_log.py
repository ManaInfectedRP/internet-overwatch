"""Event feed and incident detail dialog (plan sections 13, 11, 90)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.defaults import EventType, NodeStatus
from app.storage.models import Event, Incident
from app.ui.theme import PALETTE, monospace_family
from app.ui.widgets.status_card import Card
from app.utils.time import format_clock


class EventList(QListWidget):
    """Newest-first list of events; each row opens its details."""

    event_activated = Signal(object)  # Event

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.itemActivated.connect(self._on_activated)
        self.itemClicked.connect(self._on_activated)
        self._max_rows = 300

    def add_event(self, event: Event, show_millis: bool = False) -> None:
        item = QListWidgetItem(self._format(event, show_millis))
        item.setData(Qt.ItemDataRole.UserRole, event)
        item.setForeground(self._color(event))
        item.setToolTip(self._tooltip(event))
        self.insertItem(0, item)
        while self.count() > self._max_rows:
            self.takeItem(self.count() - 1)

    def set_events(self, events: list[Event], show_millis: bool = False) -> None:
        self.clear()
        for event in reversed(events):
            self.add_event(event, show_millis)

    @staticmethod
    def _format(event: Event, show_millis: bool) -> str:
        status = event.status
        name = event.target_name or event.metadata.get("target_name", "")
        prefix = f"{format_clock(event.timestamp, show_millis)}  {status.symbol}"
        if name and name not in event.message:
            return f"{prefix}  [{name}] {event.message}"
        return f"{prefix}  {event.message}"

    @staticmethod
    def _color(event: Event):
        from PySide6.QtGui import QColor

        return QColor(PALETTE.status_color(event.status))

    @staticmethod
    def _tooltip(event: Event) -> str:
        lines = [
            f"{event.type_enum.label}",
            f"Time: {format_clock(event.timestamp, True)}",
        ]
        if event.severity:
            lines.append(f"Severity: {event.severity.capitalize()}")
        if event.type_enum == EventType.INCIDENT:
            lines.append("Click for full incident details")
        return "\n".join(lines)

    def _on_activated(self, item: QListWidgetItem) -> None:
        event = item.data(Qt.ItemDataRole.UserRole)
        if event is not None:
            self.event_activated.emit(event)


class EventLogCard(Card):
    """The 'Recent events' card."""

    event_activated = Signal(object)

    def __init__(self, title: str = "Recent events", parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.list = EventList()
        self.list.event_activated.connect(self.event_activated)
        self.add(self.list, 1)

        self.empty_label = QLabel("Events appear here as they are detected.")
        self.empty_label.setObjectName("Faint")
        self.add(self.empty_label)

    def add_event(self, event: Event, show_millis: bool = False) -> None:
        self.list.add_event(event, show_millis)
        self.empty_label.setVisible(self.list.count() == 0)

    def set_events(self, events: list[Event], show_millis: bool = False) -> None:
        self.list.set_events(events, show_millis)
        self.empty_label.setVisible(self.list.count() == 0)

    def clear(self) -> None:
        self.list.clear()
        self.empty_label.setVisible(True)


class IncidentDialog(QDialog):
    """Full detail for one incident (plan sections 11, 90)."""

    def __init__(self, incident: Incident, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from app.services.report_service import build_incident_report

        self.setWindowTitle(f"{incident.severity.label} incident - {incident.target_name}")
        self.setMinimumSize(520, 470)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QHBoxLayout()
        status = (
            NodeStatus.PROBLEM if incident.severity.rank >= 3 else NodeStatus.WARNING
        )
        title = QLabel(f"{status.symbol}  {incident.severity.label.upper()} INCIDENT")
        title.setStyleSheet(
            f"color: {PALETTE.severity_color(incident.severity)}; "
            f"font-size: 13pt; font-weight: 700;"
        )
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(build_incident_report(incident))
        text.setStyleSheet(
            f"font-family: '{monospace_family()}'; background-color: {PALETTE.surface};"
        )
        layout.addWidget(text, 1)

        note = QLabel(
            "Conclusions are based on comparing all monitored targets inside a short "
            "window around the incident. They indicate the most likely layer, not proof."
        )
        note.setWordWrap(True)
        note.setObjectName("Faint")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class EventDialog(QDialog):
    """Detail for a non-incident event."""

    def __init__(self, event: Event, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(event.type_enum.label)
        self.setMinimumSize(440, 320)

        layout = QVBoxLayout(self)
        title = QLabel(f"{event.status.symbol}  {event.type_enum.label}")
        title.setStyleSheet(
            f"color: {PALETTE.status_color(event.status)}; font-size: 12pt; font-weight: 700;"
        )
        layout.addWidget(title)

        lines = [
            f"Time:     {format_clock(event.timestamp, True)}",
            f"Message:  {event.message}",
        ]
        if event.severity:
            lines.append(f"Severity: {event.severity.capitalize()}")
        target = event.target_name or event.metadata.get("target_name", "")
        if target:
            lines.append(f"Target:   {target}")
        for key, value in event.metadata.items():
            if key in ("target_name",) or isinstance(value, (dict, list)):
                continue
            lines.append(f"{key + ':':<10}{value}")

        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setStyleSheet(f"font-family: '{monospace_family()}';")
        layout.addWidget(text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def show_event_details(event: Event, parent: QWidget | None = None) -> None:
    """Open the right dialog for an event, incident-aware."""
    if event.type_enum == EventType.INCIDENT and event.metadata:
        incident = Incident.from_metadata(event.metadata)
        IncidentDialog(incident, parent).exec()
    else:
        EventDialog(event, parent).exec()
