"""Persisted application settings.

Settings live in a single JSON file next to the database. The dataclasses below
mirror the settings categories from plan section 56 and carry the defaults from
`app.config.defaults`, so a missing or partial file always yields a usable
configuration rather than an error.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from app.config import defaults as D
from app.utils.logger import get_logger
from app.utils.platform import user_data_dir

log = get_logger("config.settings")


def _coerce(value: Any, target_type: Any) -> Any:
    """Best-effort conversion of a loaded JSON value into the field type."""
    if target_type in (int, float, str, bool):
        try:
            if target_type is bool:
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "on")
                return bool(value)
            return target_type(value)
        except (TypeError, ValueError):
            return value
    return value


@dataclass
class MonitoringSettings:
    """Plan section 56 - Monitoring."""

    gateway_interval_ms: int = D.DEFAULT_GATEWAY_INTERVAL_MS
    internet_interval_ms: int = D.DEFAULT_INTERNET_INTERVAL_MS
    custom_interval_ms: int = D.DEFAULT_CUSTOM_INTERVAL_MS
    timeout_ms: int = D.DEFAULT_TIMEOUT_MS
    ip_version: str = D.IPVersion.AUTO.value
    auto_start_monitoring: bool = True
    system_sampling_enabled: bool = True

    def clamp(self) -> None:
        self.gateway_interval_ms = max(D.MIN_INTERVAL_MS, int(self.gateway_interval_ms))
        self.internet_interval_ms = max(D.MIN_INTERVAL_MS, int(self.internet_interval_ms))
        self.custom_interval_ms = max(D.MIN_INTERVAL_MS, int(self.custom_interval_ms))
        self.timeout_ms = max(100, int(self.timeout_ms))

    def interval_for(self, category: str) -> int:
        return {
            D.TargetCategory.GATEWAY.value: self.gateway_interval_ms,
            D.TargetCategory.INTERNET.value: self.internet_interval_ms,
            D.TargetCategory.CUSTOM.value: self.custom_interval_ms,
        }.get(category, self.custom_interval_ms)


@dataclass
class DetectionSettings:
    """Plan sections 20-23 - spike/loss/jitter sensitivity."""

    spike_absolute_ms: float = D.DEFAULT_SPIKE_ABSOLUTE_MS
    spike_multiplier: float = D.DEFAULT_SPIKE_MULTIPLIER
    rolling_window: int = D.DEFAULT_ROLLING_WINDOW
    incident_gap_seconds: float = D.INCIDENT_GAP_SECONDS
    loss_minor: float = D.LOSS_MINOR
    loss_warning: float = D.LOSS_WARNING
    loss_serious: float = D.LOSS_SERIOUS
    loss_window_samples: int = D.LOSS_WINDOW_SAMPLES
    jitter_excellent_ms: float = D.JITTER_EXCELLENT
    jitter_good_ms: float = D.JITTER_GOOD
    jitter_warning_ms: float = D.JITTER_WARNING
    severity_minor_ms: float = D.SEVERITY_THRESHOLDS_MS[D.Severity.MINOR]
    severity_moderate_ms: float = D.SEVERITY_THRESHOLDS_MS[D.Severity.MODERATE]
    severity_severe_ms: float = D.SEVERITY_THRESHOLDS_MS[D.Severity.SEVERE]
    severity_critical_ms: float = D.SEVERITY_THRESHOLDS_MS[D.Severity.CRITICAL]

    def severity_thresholds(self) -> dict[D.Severity, float]:
        return {
            D.Severity.MINOR: self.severity_minor_ms,
            D.Severity.MODERATE: self.severity_moderate_ms,
            D.Severity.SEVERE: self.severity_severe_ms,
            D.Severity.CRITICAL: self.severity_critical_ms,
        }

    def clamp(self) -> None:
        self.spike_absolute_ms = max(1.0, float(self.spike_absolute_ms))
        self.spike_multiplier = max(1.05, float(self.spike_multiplier))
        self.rolling_window = max(D.MIN_BASELINE_SAMPLES, int(self.rolling_window))
        self.loss_window_samples = max(5, int(self.loss_window_samples))


@dataclass
class AppearanceSettings:
    """Plan section 56 - Appearance."""

    theme: str = "dark"
    scale: float = 1.0
    compact_mode: bool = False
    language: str = "en"
    show_millis_in_events: bool = True


@dataclass
class NotificationSettings:
    """Plan section 43."""

    enabled: bool = True
    minimum_severity: str = D.DEFAULT_MIN_NOTIFY_SEVERITY.value
    cooldown_seconds: int = D.DEFAULT_NOTIFICATION_COOLDOWN_S
    sound_enabled: bool = False  # plan section 44: default OFF


@dataclass
class StorageSettings:
    """Plan section 40."""

    retention_days: int = D.DEFAULT_RETENTION_DAYS
    database_path: str = ""  # empty means default location
    flush_seconds: float = D.DB_FLUSH_SECONDS

    def resolved_database_path(self) -> Path:
        if self.database_path:
            return Path(self.database_path).expanduser()
        return user_data_dir() / D.DATABASE_FILENAME


@dataclass
class AdvancedSettings:
    """Plan section 56 - Advanced."""

    traceroute_command: str = ""  # empty means auto-detect per platform
    ping_implementation: str = "auto"  # auto | system | socket
    debug_logging: bool = False
    dns_resolvers: list[str] = field(default_factory=lambda: list(D.DEFAULT_DNS_RESOLVERS))
    dns_probe_host: str = D.DEFAULT_DNS_PROBE_HOST

    @property
    def log_level(self) -> str:
        return "DEBUG" if self.debug_logging else "INFO"


@dataclass
class GamingSettings:
    """Plan section 46 - gaming mode profile."""

    enabled: bool = False
    reduce_ui_updates: bool = True
    disable_heavy_diagnostics: bool = True
    prioritize_custom_target: bool = True
    show_overlay: bool = False
    overlay_opacity: float = 0.85
    overlay_font_size: int = 14
    overlay_x: int = 40
    overlay_y: int = 40
    overlay_metrics: list[str] = field(
        default_factory=lambda: ["latency", "jitter", "loss", "status"]
    )


@dataclass
class Settings:
    """Root settings object persisted as JSON."""

    monitoring: MonitoringSettings = field(default_factory=MonitoringSettings)
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    advanced: AdvancedSettings = field(default_factory=AdvancedSettings)
    gaming: GamingSettings = field(default_factory=GamingSettings)
    first_run_completed: bool = False

    # ---------------------------------------------------------------- io ---
    @staticmethod
    def config_path() -> Path:
        return user_data_dir() / D.CONFIG_FILENAME

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        settings = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            raw = data[f.name]
            current = getattr(settings, f.name)
            if is_dataclass(current) and isinstance(raw, dict):
                for sub in fields(current):
                    if sub.name in raw:
                        setattr(current, sub.name, _coerce(raw[sub.name], sub.type))
            else:
                setattr(settings, f.name, _coerce(raw, f.type))
        settings.validate()
        return settings

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or cls.config_path()
        if not path.exists():
            log.info("No settings file at %s, using defaults", path)
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read settings (%s); falling back to defaults", exc)
            return cls()
        try:
            return cls.from_dict(data)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Malformed settings (%s); falling back to defaults", exc)
            return cls()

    def save(self, path: Path | None = None) -> bool:
        path = path or self.config_path()
        self.validate()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
            log.debug("Settings saved to %s", path)
            return True
        except OSError as exc:
            log.error("Could not save settings: %s", exc)
            return False

    def validate(self) -> None:
        self.monitoring.clamp()
        self.detection.clamp()
        self.storage.retention_days = max(0, int(self.storage.retention_days))
        self.appearance.scale = min(2.0, max(0.75, float(self.appearance.scale)))
        self.notifications.cooldown_seconds = max(0, int(self.notifications.cooldown_seconds))
        if self.monitoring.ip_version not in {v.value for v in D.IPVersion}:
            self.monitoring.ip_version = D.IPVersion.AUTO.value
        if self.notifications.minimum_severity not in {s.value for s in D.Severity}:
            self.notifications.minimum_severity = D.DEFAULT_MIN_NOTIFY_SEVERITY.value

    # ------------------------------------------------------------ helpers ---
    @property
    def min_notify_severity(self) -> D.Severity:
        return D.Severity(self.notifications.minimum_severity)

    @property
    def ip_version(self) -> D.IPVersion:
        return D.IPVersion(self.monitoring.ip_version)

    def ui_refresh_ms(self) -> int:
        if self.gaming.enabled and self.gaming.reduce_ui_updates:
            return D.GAMING_UI_REFRESH_MS
        return D.UI_REFRESH_MS


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.load()
    return _SETTINGS


def set_settings(settings: Settings) -> None:
    global _SETTINGS
    _SETTINGS = settings


def reset_settings() -> Settings:
    """Restore defaults and persist them."""
    settings = Settings()
    settings.save()
    set_settings(settings)
    return settings
