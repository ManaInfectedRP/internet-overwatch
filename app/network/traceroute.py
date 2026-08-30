"""Traceroute support (plan section 29).

Uses the platform tool (`tracert` on Windows, `traceroute` elsewhere) and
streams hops back through a callback so the UI can fill the table as the trace
progresses instead of freezing until it finishes.

Traceroute latency is never treated as proof that a hop is broken - routers
routinely deprioritise ICMP - and the UI repeats that caveat.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from app.utils.logger import get_logger
from app.utils.platform import IS_WINDOWS, no_window_kwargs
from app.utils.time import now_ts

log = get_logger("network.traceroute")

HopCallback = Callable[["Hop"], None]

TRACEROUTE_CAVEAT = (
    "Routers often deprioritise or rate-limit ICMP, so a slow hop is not by "
    "itself proof that the hop is faulty. Compare the trend across hops."
)


@dataclass(slots=True)
class Hop:
    number: int
    host: str | None = None
    ip: str | None = None
    times_ms: list[float] = field(default_factory=list)

    @property
    def responded(self) -> bool:
        return bool(self.times_ms) or bool(self.ip)

    @property
    def best_ms(self) -> float | None:
        return min(self.times_ms) if self.times_ms else None

    @property
    def average_ms(self) -> float | None:
        return sum(self.times_ms) / len(self.times_ms) if self.times_ms else None

    @property
    def display_host(self) -> str:
        if self.host and self.ip and self.host != self.ip:
            return f"{self.host} ({self.ip})"
        return self.host or self.ip or "*"

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "host": self.host,
            "ip": self.ip,
            "times_ms": list(self.times_ms),
            "best_ms": self.best_ms,
            "average_ms": self.average_ms,
        }


@dataclass(slots=True)
class TraceResult:
    target: str
    hops: list[Hop] = field(default_factory=list)
    completed: bool = False
    error: str | None = None
    timestamp: float = field(default_factory=now_ts)

    @property
    def signature(self) -> list[str]:
        """Route fingerprint used to detect route changes (plan section 73)."""
        return [hop.ip or "*" for hop in self.hops]

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "timestamp": self.timestamp,
            "completed": self.completed,
            "error": self.error,
            "hops": [hop.to_dict() for hop in self.hops],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TraceResult":
        result = cls(
            target=data.get("target", ""),
            completed=bool(data.get("completed")),
            error=data.get("error"),
            timestamp=float(data.get("timestamp") or now_ts()),
        )
        for raw in data.get("hops", []):
            result.hops.append(
                Hop(
                    number=int(raw.get("number", 0)),
                    host=raw.get("host"),
                    ip=raw.get("ip"),
                    times_ms=[float(t) for t in raw.get("times_ms", [])],
                )
            )
        return result


_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{2,}:[0-9a-fA-F:]*)")
_TIME_RE = re.compile(r"([\d.,]+)\s*ms", re.IGNORECASE)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def parse_hop_line(line: str) -> Hop | None:
    """Parse one line of tracert/traceroute output into a :class:`Hop`.

    Handles both layouts::

        Windows:  3    14 ms    13 ms    14 ms  isp-gw.example [10.0.0.1]
        Unix:     3  isp-gw.example (10.0.0.1)  14.2 ms  13.9 ms  14.4 ms
    """
    stripped = line.strip()
    if not stripped:
        return None
    match = re.match(r"^(\d{1,3})\s+(.*)$", stripped)
    if not match:
        return None
    number = int(match.group(1))
    rest = match.group(2)

    times = []
    for raw in _TIME_RE.findall(rest):
        try:
            times.append(float(raw.replace(",", ".")))
        except ValueError:
            continue

    ip = None
    host = None
    bracket = re.search(r"\[([^\]]+)\]", rest)
    paren = re.search(r"\(([^)]+)\)", rest)
    if bracket and _is_ip(bracket.group(1)):
        ip = bracket.group(1)
    elif paren and _is_ip(paren.group(1)):
        ip = paren.group(1)

    # Hostname: the token immediately before the address, if it is not a number.
    name_match = re.search(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*[\[(]", rest)
    if name_match:
        host = name_match.group(1)
    if ip is None:
        # No bracketed address: take the last bare IP on the line.
        candidates = [c for c in _IP_RE.findall(rest) if _is_ip(c)]
        if candidates:
            ip = candidates[-1]
    if host is None and ip:
        host = ip
    if host is None and "*" not in rest and ip is None:
        tokens = [t for t in rest.split() if t not in ("ms",) and not _TIME_RE.fullmatch(t)]
        if tokens and not tokens[-1].replace(".", "").isdigit():
            host = tokens[-1]

    return Hop(number=number, host=host, ip=ip, times_ms=times)


def traceroute_command(target: str, max_hops: int, timeout_ms: int,
                       custom: str = "") -> list[str]:
    """Build the platform traceroute command line."""
    if custom.strip():
        parts = custom.strip().split()
        return [part.replace("{target}", target) for part in parts] + (
            [] if "{target}" in custom else [target]
        )
    if IS_WINDOWS:
        return ["tracert", "-h", str(max_hops), "-w", str(max(100, timeout_ms)), target]
    seconds = max(1, int(round(timeout_ms / 1000.0)))
    return ["traceroute", "-m", str(max_hops), "-w", str(seconds), "-q", "3", target]


def run_traceroute_stream(
    target: str,
    max_hops: int = 30,
    timeout_ms: int = 2000,
    custom_command: str = "",
    stop_event: threading.Event | None = None,
) -> Iterator[Hop]:
    """Yield hops as the traceroute process produces them."""
    cmd = traceroute_command(target, max_hops, timeout_ms, custom_command)
    log.info("Running traceroute: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **no_window_kwargs(),
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"traceroute unavailable: {exc}") from exc

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if stop_event is not None and stop_event.is_set():
                proc.terminate()
                break
            line = raw.decode("utf-8", errors="replace")
            hop = parse_hop_line(line)
            if hop is not None:
                yield hop
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
            proc.kill()


def run_traceroute(
    target: str,
    max_hops: int = 30,
    timeout_ms: int = 2000,
    custom_command: str = "",
    on_hop: HopCallback | None = None,
    stop_event: threading.Event | None = None,
) -> TraceResult:
    """Run a full traceroute, optionally reporting hops as they arrive."""
    result = TraceResult(target=target)
    try:
        for hop in run_traceroute_stream(target, max_hops, timeout_ms,
                                         custom_command, stop_event):
            result.hops.append(hop)
            if on_hop is not None:
                try:
                    on_hop(hop)
                except Exception as exc:  # pragma: no cover - UI callback safety
                    log.debug("traceroute callback error: %s", exc)
        result.completed = stop_event is None or not stop_event.is_set()
    except RuntimeError as exc:
        result.error = str(exc)
        log.warning("Traceroute failed: %s", exc)
    return result


def compare_routes(previous: TraceResult, current: TraceResult) -> dict:
    """Diff two traces to spot a route change (plan section 73)."""
    old = previous.signature
    new = current.signature
    changed_at: list[int] = []
    for index in range(max(len(old), len(new))):
        old_hop = old[index] if index < len(old) else None
        new_hop = new[index] if index < len(new) else None
        if old_hop != new_hop and not (old_hop == "*" or new_hop == "*"):
            changed_at.append(index + 1)
    return {
        "changed": bool(changed_at),
        "changed_hops": changed_at,
        "previous": old,
        "current": new,
        "previous_timestamp": previous.timestamp,
        "current_timestamp": current.timestamp,
    }
