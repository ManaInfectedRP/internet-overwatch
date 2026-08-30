"""Dark theme and shared visual language (plan sections 53, 54, 91).

Colour carries state only - green/amber/red/grey - and is always paired with a
text label so the UI stays readable for colour-blind users. Everything else is
neutral greys with a single restrained accent.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase

from app.config.defaults import NodeStatus, Severity


@dataclass(frozen=True)
class Palette:
    """Named colours; the whole app reads from this one object."""

    background: str = "#0F1216"
    surface: str = "#161A20"
    surface_alt: str = "#1C2128"
    surface_hover: str = "#232A33"
    border: str = "#262D36"
    border_strong: str = "#333C47"

    text: str = "#E6EAF0"
    text_muted: str = "#93A1B1"
    text_faint: str = "#64717F"

    accent: str = "#4C9AFF"
    accent_dim: str = "#2A5FA8"

    healthy: str = "#4CAF6D"
    warning: str = "#E5A83B"
    problem: str = "#E5544B"
    unknown: str = "#6B7785"

    gateway: str = "#4FC3F7"
    internet: str = "#81C784"
    custom: str = "#FFB74D"

    grid: str = "#222933"
    graph_bg: str = "#12161B"

    def status_color(self, status: NodeStatus) -> str:
        return {
            NodeStatus.HEALTHY: self.healthy,
            NodeStatus.WARNING: self.warning,
            NodeStatus.PROBLEM: self.problem,
            NodeStatus.UNKNOWN: self.unknown,
        }[status]

    def severity_color(self, severity: Severity | None) -> str:
        if severity is None:
            return self.unknown
        return {
            Severity.MINOR: self.warning,
            Severity.MODERATE: self.warning,
            Severity.SEVERE: self.problem,
            Severity.CRITICAL: "#FF6E63",
        }[severity]

    def qcolor(self, value: str, alpha: int = 255) -> QColor:
        color = QColor(value)
        color.setAlpha(alpha)
        return color


PALETTE = Palette()

# Radii and spacing used across widgets so cards line up.
RADIUS = 10
RADIUS_SMALL = 6
SPACING = 12
CARD_PADDING = 14


def preferred_font_family() -> str:
    families = set(QFontDatabase.families())
    for candidate in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Ubuntu",
                      "Noto Sans", "DejaVu Sans"):
        if candidate in families:
            return candidate
    return "sans-serif"


def monospace_family() -> str:
    families = set(QFontDatabase.families())
    for candidate in ("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono",
                      "Menlo", "Courier New"):
        if candidate in families:
            return candidate
    return "monospace"


def scaled_font(point_size: float, weight: QFont.Weight | int = QFont.Weight.Normal,
                scale: float = 1.0, mono: bool = False) -> QFont:
    font = QFont(monospace_family() if mono else preferred_font_family())
    font.setPointSizeF(max(6.0, point_size * scale))
    if not isinstance(weight, QFont.Weight):
        # Accept a plain CSS-style number (400, 600, 700) for convenience.
        requested = int(weight)
        weight = min(
            QFont.Weight,
            key=lambda candidate: abs(int(candidate.value) - requested),
        )
    font.setWeight(weight)
    return font


def stylesheet(palette: Palette = PALETTE, scale: float = 1.0) -> str:
    """Application-wide stylesheet."""
    base = 10 * scale
    return f"""
    QWidget {{
        background-color: {palette.background};
        color: {palette.text};
        font-family: "{preferred_font_family()}";
        font-size: {base:.1f}pt;
    }}

    QMainWindow, QDialog {{
        background-color: {palette.background};
    }}

    /* Text widgets must not paint the page background over a card surface. */
    QLabel, QCheckBox, QRadioButton, QGroupBox {{
        background: transparent;
    }}

    QToolTip {{
        background-color: {palette.surface_alt};
        color: {palette.text};
        border: 1px solid {palette.border_strong};
        padding: 6px;
        border-radius: {RADIUS_SMALL}px;
    }}

    /* ---------------------------------------------------------- sidebar -- */
    #Sidebar {{
        background-color: {palette.surface};
        border-right: 1px solid {palette.border};
    }}

    #SidebarButton {{
        background-color: transparent;
        border: none;
        border-left: 3px solid transparent;
        color: {palette.text_muted};
        padding: 11px 16px;
        text-align: left;
        font-size: {base + 0.5:.1f}pt;
        border-radius: 0px;
    }}
    #SidebarButton:hover {{
        background-color: {palette.surface_hover};
        color: {palette.text};
    }}
    #SidebarButton:checked {{
        background-color: {palette.surface_alt};
        border-left: 3px solid {palette.accent};
        color: {palette.text};
        font-weight: 600;
    }}

    #SidebarTitle {{
        color: {palette.text};
        font-size: {base + 3:.1f}pt;
        font-weight: 700;
        padding: 18px 16px 4px 16px;
    }}
    #SidebarSubtitle {{
        color: {palette.text_faint};
        font-size: {base - 1.5:.1f}pt;
        padding: 0px 16px 16px 16px;
    }}

    /* ------------------------------------------------------------ cards -- */
    #Card {{
        background-color: {palette.surface};
        border: 1px solid {palette.border};
        border-radius: {RADIUS}px;
    }}
    #CardTitle {{
        color: {palette.text_faint};
        font-size: {base - 1:.1f}pt;
        font-weight: 600;
        letter-spacing: 1px;
    }}
    #MetricValue {{
        color: {palette.text};
        font-size: {base + 14:.1f}pt;
        font-weight: 600;
    }}
    #MetricLabel {{
        color: {palette.text_faint};
        font-size: {base - 1:.1f}pt;
        letter-spacing: 1px;
    }}
    #MetricSub {{
        color: {palette.text_muted};
        font-size: {base - 1:.1f}pt;
    }}
    #Muted {{
        color: {palette.text_muted};
    }}
    #Faint {{
        color: {palette.text_faint};
    }}
    #SectionTitle {{
        color: {palette.text_faint};
        font-size: {base - 0.5:.1f}pt;
        font-weight: 700;
        letter-spacing: 1.4px;
    }}
    #PageTitle {{
        color: {palette.text};
        font-size: {base + 7:.1f}pt;
        font-weight: 700;
    }}

    /* --------------------------------------------------------- controls -- */
    QPushButton {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border_strong};
        border-radius: {RADIUS_SMALL}px;
        padding: 7px 14px;
        color: {palette.text};
    }}
    QPushButton:hover {{
        background-color: {palette.surface_hover};
        border-color: {palette.accent_dim};
    }}
    QPushButton:pressed {{
        background-color: {palette.border};
    }}
    QPushButton:disabled {{
        color: {palette.text_faint};
        border-color: {palette.border};
    }}
    QPushButton#Primary {{
        background-color: {palette.accent_dim};
        border-color: {palette.accent};
        color: #FFFFFF;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{
        background-color: {palette.accent};
    }}
    QPushButton#Danger {{
        border-color: {palette.problem};
        color: {palette.problem};
    }}
    QPushButton#Danger:hover {{
        background-color: {palette.problem};
        color: #FFFFFF;
    }}
    QPushButton#Toggle {{
        padding: 5px 10px;
        font-size: {base - 1:.1f}pt;
    }}
    QPushButton#Toggle:checked {{
        background-color: {palette.accent_dim};
        border-color: {palette.accent};
        color: #FFFFFF;
    }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border_strong};
        border-radius: {RADIUS_SMALL}px;
        padding: 6px 8px;
        selection-background-color: {palette.accent_dim};
        color: {palette.text};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border-color: {palette.accent};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border_strong};
        selection-background-color: {palette.accent_dim};
        color: {palette.text};
        outline: none;
    }}

    QCheckBox, QRadioButton {{
        spacing: 8px;
        color: {palette.text};
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {palette.border_strong};
        border-radius: 3px;
        background-color: {palette.surface_alt};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {palette.accent};
        border-color: {palette.accent};
    }}
    QRadioButton::indicator {{
        border-radius: 8px;
    }}

    QSlider::groove:horizontal {{
        height: 4px;
        background: {palette.border};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {palette.accent};
        width: 14px;
        margin: -6px 0;
        border-radius: 7px;
    }}

    /* ----------------------------------------------------------- tables -- */
    QTableWidget, QTableView, QTreeWidget, QListWidget {{
        background-color: {palette.surface};
        alternate-background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: {RADIUS_SMALL}px;
        gridline-color: {palette.border};
        selection-background-color: {palette.accent_dim};
        selection-color: #FFFFFF;
        outline: none;
    }}
    QHeaderView::section {{
        background-color: {palette.surface_alt};
        color: {palette.text_faint};
        border: none;
        border-bottom: 1px solid {palette.border_strong};
        padding: 7px 8px;
        font-weight: 600;
    }}
    QTableWidget::item, QTreeWidget::item, QListWidget::item {{
        padding: 5px 4px;
        border: none;
    }}

    /* --------------------------------------------------------- scrolling -- */
    QScrollArea {{
        border: none;
        background-color: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {palette.border_strong};
        border-radius: 5px;
        min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {palette.text_faint};
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
    }}
    QScrollBar::handle:horizontal {{
        background: {palette.border_strong};
        border-radius: 5px;
        min-width: 28px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0px;
        width: 0px;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: transparent;
    }}

    /* ------------------------------------------------------------- misc -- */
    QGroupBox {{
        border: 1px solid {palette.border};
        border-radius: {RADIUS}px;
        margin-top: 14px;
        padding-top: 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 5px;
        color: {palette.text_faint};
    }}
    QProgressBar {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border};
        border-radius: {RADIUS_SMALL}px;
        text-align: center;
        color: {palette.text};
        height: 16px;
    }}
    QProgressBar::chunk {{
        background-color: {palette.accent};
        border-radius: {RADIUS_SMALL - 1}px;
    }}
    QSplitter::handle {{
        background-color: {palette.border};
    }}
    QTabWidget::pane {{
        border: 1px solid {palette.border};
        border-radius: {RADIUS_SMALL}px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {palette.text_muted};
        padding: 8px 14px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{
        color: {palette.text};
        border-bottom: 2px solid {palette.accent};
    }}
    QMenu {{
        background-color: {palette.surface_alt};
        border: 1px solid {palette.border_strong};
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 22px 6px 12px;
        border-radius: 4px;
    }}
    QMenu::item:selected {{
        background-color: {palette.accent_dim};
    }}
    QStatusBar {{
        background-color: {palette.surface};
        border-top: 1px solid {palette.border};
        color: {palette.text_muted};
    }}
    """


def status_chip_style(status: NodeStatus, palette: Palette = PALETTE) -> str:
    """Inline style for a small status pill."""
    color = palette.status_color(status)
    return (
        f"color: {color}; border: 1px solid {color}; border-radius: {RADIUS_SMALL}px; "
        f"padding: 2px 8px; font-weight: 600;"
    )
