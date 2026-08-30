"""Time helpers — monotonic sampling clock plus human formatting."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ts() -> float:
    """Wall-clock UNIX timestamp used for stored samples."""
    return time.time()


def monotonic() -> float:
    """Monotonic clock used for measuring durations and scheduling."""
    return time.monotonic()


def to_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


def utc_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def format_clock(ts: float, with_millis: bool = False) -> str:
    dt = to_datetime(ts)
    if with_millis:
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return dt.strftime("%H:%M:%S")


def format_datetime(ts: float) -> str:
    return to_datetime(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_date(ts: float) -> str:
    return to_datetime(ts).strftime("%Y-%m-%d")


def format_duration(seconds: float) -> str:
    """Human duration: 42s, 2m 13s, 2h 14m, 3d 4h."""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def format_latency(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 10:
        return f"{ms:.1f} ms"
    return f"{ms:.0f} ms"


def format_bps(bits_per_second: float | None) -> str:
    if bits_per_second is None:
        return "—"
    bps = float(bits_per_second)
    for unit, factor in (("Gbps", 1e9), ("Mbps", 1e6), ("Kbps", 1e3)):
        if bps >= factor:
            return f"{bps / factor:.1f} {unit}"
    return f"{bps:.0f} bps"


def format_bytes(num: float | None) -> str:
    if num is None:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return f"{value:.1f} TB"
