"""Default configuration values and shared enumerations.

Every threshold in Internet Overwatch lives here rather than being scattered
through the code, so behaviour can be tuned in one place (plan sections 67, 95).
"""

from __future__ import annotations

from enum import Enum

APP_NAME = "Internet Overwatch"
APP_ID = "internet-overwatch"
CONFIG_FILENAME = "settings.json"
DATABASE_FILENAME = "overwatch.db"


class TargetCategory(str, Enum):
    """Which network layer a target measures (plan section 3)."""

    GATEWAY = "gateway"
    INTERNET = "internet"
    CUSTOM = "custom"

    @property
    def label(self) -> str:
        return {
            TargetCategory.GATEWAY: "Router",
            TargetCategory.INTERNET: "Internet",
            TargetCategory.CUSTOM: "Game / Custom",
        }[self]


class Protocol(str, Enum):
    """How a target is probed (plan section 28)."""

    ICMP = "icmp"
    TCP = "tcp"
    DNS = "dns"

    @property
    def label(self) -> str:
        return {
            Protocol.ICMP: "ICMP ping",
            Protocol.TCP: "TCP connect",
            Protocol.DNS: "DNS query",
        }[self]

    @property
    def measurement_note(self) -> str:
        """Shown in the UI so a measurement is never mistaken for in-game ping."""
        return {
            Protocol.ICMP: "ICMP echo round-trip time",
            Protocol.TCP: "TCP handshake time - not identical to in-game latency",
            Protocol.DNS: "DNS query response time",
        }[self]


class Severity(str, Enum):
    """Spike severity buckets (plan section 23)."""

    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"minor": 1, "moderate": 2, "severe": 3, "critical": 4}[self.value]

    @property
    def label(self) -> str:
        return self.value.capitalize()

    def __lt__(self, other):  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other):  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other):  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other):  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank >= other.rank


class EventType(str, Enum):
    """Kinds of entries shown in the event feed (plan section 13)."""

    MONITORING_STARTED = "monitoring_started"
    MONITORING_STOPPED = "monitoring_stopped"
    SPIKE = "spike"
    INCIDENT = "incident"
    PACKET_LOSS = "packet_loss"
    HIGH_JITTER = "high_jitter"
    TARGET_UNREACHABLE = "target_unreachable"
    TARGET_RECOVERED = "target_recovered"
    INTERNET_UNREACHABLE = "internet_unreachable"
    LOCAL_NETWORK_UNREACHABLE = "local_network_unreachable"
    STABILIZED = "stabilized"
    ROUTE_CHANGED = "route_changed"
    INFO = "info"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


class HealthStatus(str, Enum):
    """Health score bands (plan section 8.1)."""

    EXCELLENT = "excellent"
    GOOD = "good"
    STABLE = "stable"
    UNSTABLE = "unstable"
    POOR = "poor"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        return self.value.upper()


class Confidence(str, Enum):
    """Diagnostic confidence (plan section 68)."""

    LIKELY = "likely"
    POSSIBLE = "possible"
    UNCLEAR = "unclear"

    @property
    def label(self) -> str:
        return {"likely": "High", "possible": "Medium", "unclear": "Low"}[self.value]

    @property
    def wording(self) -> str:
        return {
            "likely": "Likely cause",
            "possible": "Possible cause",
            "unclear": "No clear cause detected",
        }[self.value]


class NodeStatus(str, Enum):
    """Traffic-light status for path nodes and targets (plan section 12)."""

    HEALTHY = "healthy"
    WARNING = "warning"
    PROBLEM = "problem"
    UNKNOWN = "unknown"

    @property
    def symbol(self) -> str:
        return {
            "healthy": "\N{LARGE GREEN CIRCLE}",
            "warning": "\N{LARGE ORANGE CIRCLE}",
            "problem": "\N{LARGE RED CIRCLE}",
            "unknown": "\N{MEDIUM WHITE CIRCLE}",
        }[self.value]

    @property
    def label(self) -> str:
        # Colour is never the only signal (plan section 91).
        return {
            "healthy": "HEALTHY",
            "warning": "WARNING",
            "problem": "PROBLEM",
            "unknown": "UNKNOWN",
        }[self.value]

    @property
    def rank(self) -> int:
        return {"healthy": 0, "warning": 1, "problem": 2, "unknown": -1}[self.value]


class IPVersion(str, Enum):
    """IPv4 / IPv6 preference (plan section 61)."""

    AUTO = "auto"
    IPV4 = "ipv4"
    IPV6 = "ipv6"

    @property
    def label(self) -> str:
        return {"auto": "Auto", "ipv4": "Prefer IPv4", "ipv6": "Prefer IPv6"}[self.value]


# --------------------------------------------------------------------------
# Monitoring (plan section 26)
# --------------------------------------------------------------------------
MIN_INTERVAL_MS = 100  # hard safeguard: never flood a target
DEFAULT_GATEWAY_INTERVAL_MS = 250
DEFAULT_INTERNET_INTERVAL_MS = 500
DEFAULT_CUSTOM_INTERVAL_MS = 500
DEFAULT_TIMEOUT_MS = 1000

# UI / storage cadence (plan section 51)
UI_REFRESH_MS = 200
GAMING_UI_REFRESH_MS = 500
DB_FLUSH_SECONDS = 2.0
RING_BUFFER_SECONDS = 300  # 5 minutes of samples kept in memory (plan section 50)

# --------------------------------------------------------------------------
# Detection (plan sections 22, 23)
# --------------------------------------------------------------------------
DEFAULT_SPIKE_ABSOLUTE_MS = 75.0
DEFAULT_SPIKE_MULTIPLIER = 2.0
DEFAULT_ROLLING_WINDOW = 60  # samples used for the rolling median baseline
MIN_BASELINE_SAMPLES = 8  # below this the baseline is not trusted
INCIDENT_GAP_SECONDS = 3.0  # spikes closer than this merge into one incident (section 88)

SEVERITY_THRESHOLDS_MS = {
    Severity.MINOR: 50.0,
    Severity.MODERATE: 100.0,
    Severity.SEVERE: 250.0,
    Severity.CRITICAL: 500.0,
}

# Packet loss bands, fraction 0..1 (plan section 20)
LOSS_MINOR = 0.01
LOSS_WARNING = 0.03
LOSS_SERIOUS = 0.10
LOSS_WINDOW_SAMPLES = 60

# Jitter bands in ms (plan section 21)
JITTER_EXCELLENT = 10.0
JITTER_GOOD = 25.0
JITTER_WARNING = 50.0

# Gateway health (plan section 17)
GATEWAY_LATENCY_WARNING_MS = 15.0
GATEWAY_LATENCY_PROBLEM_MS = 40.0

# Generic latency bands used for target status colouring
LATENCY_WARNING_MS = 120.0
LATENCY_PROBLEM_MS = 250.0

# Correlation window around a spike, seconds (plan section 71)
CORRELATION_WINDOW_S = 2.0

# --------------------------------------------------------------------------
# Health score weights (plan section 67)
# --------------------------------------------------------------------------
HEALTH_WEIGHTS = {
    "latency": 0.25,
    "jitter": 0.20,
    "loss": 0.25,
    "spikes": 0.20,
    "local": 0.10,
}

HEALTH_BANDS = (
    (90, HealthStatus.EXCELLENT),
    (80, HealthStatus.GOOD),
    (65, HealthStatus.STABLE),
    (50, HealthStatus.UNSTABLE),
    (30, HealthStatus.POOR),
    (0, HealthStatus.CRITICAL),
)

# Latency scoring reference points: at or below `good` scores 100,
# at or above `bad` scores 0, linear in between.
HEALTH_LATENCY_GOOD_MS = 40.0
HEALTH_LATENCY_BAD_MS = 250.0
HEALTH_JITTER_GOOD_MS = 5.0
HEALTH_JITTER_BAD_MS = 60.0
HEALTH_LOSS_BAD = 0.05
HEALTH_SPIKES_PER_MIN_BAD = 4.0

# --------------------------------------------------------------------------
# Storage (plan section 40)
# --------------------------------------------------------------------------
DEFAULT_RETENTION_DAYS = 30
RETENTION_CHOICES = [
    ("7 days", 7),
    ("30 days", 30),
    ("90 days", 90),
    ("1 year", 365),
    ("Forever", 0),
]

# --------------------------------------------------------------------------
# Notifications (plan section 43)
# --------------------------------------------------------------------------
DEFAULT_NOTIFICATION_COOLDOWN_S = 60
DEFAULT_MIN_NOTIFY_SEVERITY = Severity.SEVERE

# --------------------------------------------------------------------------
# Default targets (plan section 96)
# --------------------------------------------------------------------------
DEFAULT_TARGETS = [
    {
        "name": "Router",
        "host": "auto",  # replaced by gateway detection on first run
        "port": None,
        "protocol": Protocol.ICMP.value,
        "interval_ms": DEFAULT_GATEWAY_INTERVAL_MS,
        "enabled": True,
        "category": TargetCategory.GATEWAY.value,
    },
    {
        "name": "Cloudflare",
        "host": "1.1.1.1",
        "port": None,
        "protocol": Protocol.ICMP.value,
        "interval_ms": DEFAULT_INTERNET_INTERVAL_MS,
        "enabled": True,
        "category": TargetCategory.INTERNET.value,
    },
    {
        "name": "Google DNS",
        "host": "8.8.8.8",
        "port": None,
        "protocol": Protocol.ICMP.value,
        "interval_ms": DEFAULT_INTERNET_INTERVAL_MS,
        "enabled": True,
        "category": TargetCategory.INTERNET.value,
    },
]

DEFAULT_DNS_RESOLVERS = ["1.1.1.1", "8.8.8.8"]
DEFAULT_DNS_PROBE_HOST = "example.com"

# Graph time ranges, label -> seconds (plan section 10)
LIVE_TIME_RANGES = [
    ("1 min", 60),
    ("5 min", 300),
    ("15 min", 900),
    ("1 hour", 3600),
    ("6 hours", 21600),
    ("24 hours", 86400),
    ("7 days", 604800),
]

# Historical ranges (plan section 36)
HISTORY_TIME_RANGES = [
    ("1 hour", 3600),
    ("6 hours", 21600),
    ("12 hours", 43200),
    ("24 hours", 86400),
    ("7 days", 604800),
    ("30 days", 2592000),
]

# Series colours by category, used by graphs and the path view
CATEGORY_COLORS = {
    TargetCategory.GATEWAY.value: "#4FC3F7",
    TargetCategory.INTERNET.value: "#81C784",
    TargetCategory.CUSTOM.value: "#FFB74D",
}

# Bufferbloat grading, added latency in ms (plan section 32)
BUFFERBLOAT_BANDS = [
    (30.0, "A", "Minimal bufferbloat"),
    (60.0, "B", "Mild bufferbloat"),
    (120.0, "C", "Noticeable bufferbloat"),
    (250.0, "D", "Significant bufferbloat"),
    (float("inf"), "F", "Severe bufferbloat"),
]

# Wi-Fi signal warnings (plan section 33)
WIFI_SIGNAL_WARNING_PCT = 50
WIFI_SIGNAL_PROBLEM_PCT = 30
