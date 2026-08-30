"""Latency probes.

Three probe kinds are supported (plan section 28):

* ICMP  - a real echo request. Uses a raw socket when the OS allows it and
          falls back to the platform `ping` binary otherwise.
* TCP   - connect() handshake time. Clearly labelled in the UI because it is
          not equivalent to in-game latency.
* DNS   - query response time against a resolver.

Every probe returns a :class:`ProbeResult`; failures are values, never
exceptions, so one dead target can never take down monitoring (plan section 59).
"""

from __future__ import annotations

import ipaddress
import os
import re
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.config.defaults import IPVersion, Protocol
from app.utils.logger import get_logger
from app.utils.platform import IS_WINDOWS, run_command
from app.utils.time import now_ts

log = get_logger("network.ping")

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0
ICMPV6_ECHO_REQUEST = 128
ICMPV6_ECHO_REPLY = 129


class ErrorType:
    """Stable error identifiers stored with failed samples."""

    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    DNS_FAILURE = "dns_failure"
    PERMISSION = "permission"
    REFUSED = "refused"
    NO_ROUTE = "no_route"
    INVALID_TARGET = "invalid_target"
    UNKNOWN = "unknown"

    LABELS = {
        TIMEOUT: "Request timed out",
        UNREACHABLE: "Host unreachable",
        DNS_FAILURE: "DNS resolution failed",
        PERMISSION: "Insufficient permissions",
        REFUSED: "Connection refused",
        NO_ROUTE: "No route to host",
        INVALID_TARGET: "Invalid target",
        UNKNOWN: "Unknown error",
    }

    @classmethod
    def label(cls, error: str | None) -> str:
        if not error:
            return ""
        return cls.LABELS.get(error, error)


@dataclass(slots=True)
class ProbeResult:
    """A single measurement (plan section 24)."""

    host: str
    timestamp: float
    success: bool
    latency_ms: float | None = None
    error_type: str | None = None
    protocol: str = Protocol.ICMP.value
    resolved_ip: str | None = None
    detail: str = ""

    @property
    def failed(self) -> bool:
        return not self.success


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------

_resolve_cache: dict[tuple[str, str], tuple[float, str | None, int]] = {}
_resolve_lock = threading.Lock()
RESOLVE_TTL_S = 60.0


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def ip_family(host: str) -> int | None:
    """Return AF_INET / AF_INET6 for an IP literal, else None."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    return socket.AF_INET6 if addr.version == 6 else socket.AF_INET


def resolve_host(
    host: str, ip_version: IPVersion = IPVersion.AUTO, use_cache: bool = True
) -> tuple[str | None, int]:
    """Resolve a hostname to (ip, address_family).

    Returns (None, 0) when resolution fails. Results are cached briefly so
    frequent probes do not hammer the resolver.
    """
    host = (host or "").strip()
    if not host:
        return None, 0

    family = ip_family(host)
    if family is not None:
        return host, family

    key = (host.lower(), ip_version.value)
    now = time.monotonic()
    if use_cache:
        with _resolve_lock:
            cached = _resolve_cache.get(key)
        if cached and now - cached[0] < RESOLVE_TTL_S:
            return cached[1], cached[2]

    want = {
        IPVersion.IPV4: socket.AF_INET,
        IPVersion.IPV6: socket.AF_INET6,
        IPVersion.AUTO: socket.AF_UNSPEC,
    }[ip_version]

    ip: str | None = None
    fam = 0
    try:
        infos = socket.getaddrinfo(host, None, want, socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
        if want is not socket.AF_UNSPEC:
            # Preference could not be honoured; fall back to anything available.
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except socket.gaierror:
                infos = []
    except OSError:
        infos = []

    if infos:
        # Prefer the requested family, then IPv4 for stability.
        ordered = sorted(
            infos,
            key=lambda i: (0 if want is not socket.AF_UNSPEC and i[0] == want else 1,
                           0 if i[0] == socket.AF_INET else 1),
        )
        fam = ordered[0][0]
        ip = ordered[0][4][0]

    with _resolve_lock:
        _resolve_cache[key] = (now, ip, fam)
    return ip, fam


def clear_resolve_cache() -> None:
    with _resolve_lock:
        _resolve_cache.clear()


# ---------------------------------------------------------------------------
# ICMP via raw socket
# ---------------------------------------------------------------------------


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


class RawIcmpPinger:
    """ICMP echo over a raw socket.

    Raw sockets need administrator rights on Windows and CAP_NET_RAW (or the
    unprivileged ICMP datagram socket) on Linux. Availability is probed once
    and cached; when unavailable the system `ping` binary is used instead.
    """

    _available: Optional[bool] = None
    _seq_lock = threading.Lock()
    _seq = 0

    @classmethod
    def available(cls) -> bool:
        if cls._available is None:
            cls._available = cls._probe_availability()
            if cls._available:
                log.info("Raw ICMP sockets available - using in-process ping")
            else:
                log.info("Raw ICMP sockets unavailable - using system ping binary")
        return cls._available

    @classmethod
    def _probe_availability(cls) -> bool:
        for family, sock_type, proto in cls._socket_variants(socket.AF_INET):
            try:
                sock = socket.socket(family, sock_type, proto)
            except (PermissionError, OSError):
                continue
            sock.close()
            return True
        return False

    @staticmethod
    def _socket_variants(family: int):
        proto = socket.IPPROTO_ICMP if family == socket.AF_INET else 58
        variants = []
        if not IS_WINDOWS:
            # Unprivileged ICMP datagram socket (Linux net.ipv4.ping_group_range,
            # macOS supports it by default).
            variants.append((family, socket.SOCK_DGRAM, proto))
        variants.append((family, socket.SOCK_RAW, proto))
        return variants

    @classmethod
    def _next_seq(cls) -> int:
        with cls._seq_lock:
            cls._seq = (cls._seq + 1) & 0xFFFF
            return cls._seq

    @classmethod
    def ping(cls, ip: str, family: int, timeout_s: float, payload_size: int = 32) -> ProbeResult:
        ts = now_ts()
        ident = os.getpid() & 0xFFFF
        seq = cls._next_seq()
        is_v6 = family == socket.AF_INET6
        echo_type = ICMPV6_ECHO_REQUEST if is_v6 else ICMP_ECHO_REQUEST

        payload = bytes((i & 0xFF) for i in range(payload_size))
        header = struct.pack("!BBHHH", echo_type, 0, 0, ident, seq)
        if is_v6:
            # The kernel fills in the ICMPv6 checksum.
            packet = header + payload
        else:
            checksum = _checksum(header + payload)
            packet = struct.pack("!BBHHH", echo_type, 0, checksum, ident, seq) + payload

        sock = None
        for fam, sock_type, proto in cls._socket_variants(family):
            try:
                sock = socket.socket(fam, sock_type, proto)
                break
            except (PermissionError, OSError):
                sock = None
        if sock is None:
            cls._available = False
            return ProbeResult(ip, ts, False, error_type=ErrorType.PERMISSION,
                               resolved_ip=ip, detail="raw socket unavailable")

        try:
            sock.setblocking(False)
            start = time.perf_counter()
            try:
                sock.sendto(packet, (ip, 0))
            except OSError as exc:
                return ProbeResult(ip, ts, False, error_type=_oserror_type(exc),
                                   resolved_ip=ip, detail=str(exc))

            deadline = start + timeout_s
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return ProbeResult(ip, ts, False, error_type=ErrorType.TIMEOUT,
                                       resolved_ip=ip)
                readable, _, _ = select.select([sock], [], [], remaining)
                if not readable:
                    return ProbeResult(ip, ts, False, error_type=ErrorType.TIMEOUT,
                                       resolved_ip=ip)
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError as exc:
                    return ProbeResult(ip, ts, False, error_type=_oserror_type(exc),
                                       resolved_ip=ip, detail=str(exc))
                elapsed_ms = (time.perf_counter() - start) * 1000.0

                parsed = cls._parse_reply(data, is_v6, ident, seq)
                if parsed is None:
                    continue  # not our echo reply; keep waiting
                ok, err = parsed
                if ok:
                    return ProbeResult(ip, ts, True, latency_ms=elapsed_ms, resolved_ip=ip)
                return ProbeResult(ip, ts, False, error_type=err, resolved_ip=ip)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _parse_reply(data: bytes, is_v6: bool, ident: int, seq: int):
        """Return (success, error) for our echo, or None if it is not ours."""
        if is_v6:
            icmp = data
        else:
            if len(data) < 20:
                return None
            ihl = (data[0] & 0x0F) * 4
            icmp = data[ihl:]
        if len(icmp) < 8:
            return None
        icmp_type, _code, _cks, r_id, r_seq = struct.unpack("!BBHHH", icmp[:8])

        expected_reply = ICMPV6_ECHO_REPLY if is_v6 else ICMP_ECHO_REPLY
        if icmp_type == expected_reply:
            # Datagram ICMP sockets rewrite the identifier, so only trust seq
            # when the identifier matches.
            if r_id == ident and r_seq != seq:
                return None
            return True, None
        if icmp_type in (3, 1):  # destination unreachable (v4 / v6)
            return False, ErrorType.UNREACHABLE
        if icmp_type in (11, 3) and is_v6:
            return False, ErrorType.TIMEOUT
        return None


def _oserror_type(exc: OSError) -> str:
    errno = getattr(exc, "errno", None)
    name = getattr(exc, "__class__", type(exc)).__name__
    if isinstance(exc, PermissionError):
        return ErrorType.PERMISSION
    if isinstance(exc, ConnectionRefusedError):
        return ErrorType.REFUSED
    if isinstance(exc, socket.timeout) or name == "timeout":
        return ErrorType.TIMEOUT
    text = str(exc).lower()
    if "unreachable" in text:
        return ErrorType.NO_ROUTE if "network" in text else ErrorType.UNREACHABLE
    if errno in (10065, 113):  # WSAEHOSTUNREACH / EHOSTUNREACH
        return ErrorType.UNREACHABLE
    if errno in (10051, 101):  # WSAENETUNREACH / ENETUNREACH
        return ErrorType.NO_ROUTE
    return ErrorType.UNKNOWN


# ---------------------------------------------------------------------------
# ICMP via the system ping binary
# ---------------------------------------------------------------------------

_TIME_PATTERNS = (
    re.compile(r"time[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE),
    re.compile(r"tid[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE),  # Swedish Windows
    re.compile(r"Zeit[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE),  # German Windows
    re.compile(r"temps[=<]\s*([\d.,]+)\s*ms", re.IGNORECASE),  # French Windows
    re.compile(r"=\s*([\d.,]+)\s*ms\b", re.IGNORECASE),  # generic fallback
)


def parse_system_ping_latency(output: str) -> float | None:
    """Extract the round-trip time from `ping` output.

    Windows localises its output, so several label spellings are tried before
    the generic `= NN ms` fallback.
    """
    if not output:
        return None
    for pattern in _TIME_PATTERNS:
        match = pattern.search(output)
        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                continue
    return None


def _system_ping_command(ip: str, family: int, timeout_ms: int) -> list[str]:
    timeout_ms = max(100, int(timeout_ms))
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms)]
        cmd.append("-6" if family == socket.AF_INET6 else "-4")
        cmd.append(ip)
        return cmd
    binary = "ping6" if (family == socket.AF_INET6 and not _ping_supports_dash6()) else "ping"
    seconds = max(1, int(round(timeout_ms / 1000.0)))
    cmd = [binary, "-c", "1", "-W", str(seconds)]
    if binary == "ping" and family == socket.AF_INET6:
        cmd.insert(1, "-6")
    cmd.append(ip)
    return cmd


_dash6_supported: Optional[bool] = None


def _ping_supports_dash6() -> bool:
    global _dash6_supported
    if _dash6_supported is None:
        code, out, err = run_command(["ping", "-6", "-c", "1", "::1"], timeout=3)
        _dash6_supported = code not in (-1,) and "invalid option" not in (out + err).lower()
    return _dash6_supported


def system_ping(ip: str, family: int, timeout_ms: int) -> ProbeResult:
    """Run one probe through the platform `ping` binary."""
    ts = now_ts()
    cmd = _system_ping_command(ip, family, timeout_ms)
    start = time.perf_counter()
    code, out, err = run_command(cmd, timeout=max(2.0, timeout_ms / 1000.0 + 2.0))
    wall_ms = (time.perf_counter() - start) * 1000.0
    text = out + "\n" + err

    if code == -1:
        return ProbeResult(ip, ts, False, error_type=ErrorType.UNKNOWN, resolved_ip=ip,
                           detail="ping binary not found")
    if code == -2:
        return ProbeResult(ip, ts, False, error_type=ErrorType.TIMEOUT, resolved_ip=ip)

    latency = parse_system_ping_latency(text)
    lowered = text.lower()

    # Windows prints "Reply from ...: Destination host unreachable" with exit 0.
    if latency is not None and "unreachable" not in lowered and code == 0:
        return ProbeResult(ip, ts, True, latency_ms=latency, resolved_ip=ip)
    if "unreachable" in lowered or "onaobar" in lowered:
        return ProbeResult(ip, ts, False, error_type=ErrorType.UNREACHABLE, resolved_ip=ip)
    if "could not find host" in lowered or "unknown host" in lowered or "name or service" in lowered:
        return ProbeResult(ip, ts, False, error_type=ErrorType.DNS_FAILURE, resolved_ip=ip)
    if code == 0 and latency is not None:
        return ProbeResult(ip, ts, True, latency_ms=latency, resolved_ip=ip)
    if code == 0 and wall_ms < timeout_ms:
        # Succeeded but the time could not be parsed; wall time is a fair proxy.
        return ProbeResult(ip, ts, True, latency_ms=wall_ms, resolved_ip=ip,
                           detail="latency estimated from wall clock")
    return ProbeResult(ip, ts, False, error_type=ErrorType.TIMEOUT, resolved_ip=ip)


# ---------------------------------------------------------------------------
# TCP connect
# ---------------------------------------------------------------------------


def tcp_ping(ip: str, port: int, family: int, timeout_s: float) -> ProbeResult:
    """Measure the TCP handshake time (plan section 28).

    A refused connection still proves the host is reachable, so it counts as a
    successful reachability measurement.
    """
    ts = now_ts()
    sock = socket.socket(family or socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    start = time.perf_counter()
    try:
        sock.connect((ip, int(port)))
        elapsed = (time.perf_counter() - start) * 1000.0
        return ProbeResult(ip, ts, True, latency_ms=elapsed, resolved_ip=ip,
                           protocol=Protocol.TCP.value)
    except ConnectionRefusedError:
        elapsed = (time.perf_counter() - start) * 1000.0
        return ProbeResult(ip, ts, True, latency_ms=elapsed, resolved_ip=ip,
                           protocol=Protocol.TCP.value,
                           detail="connection refused - host reachable")
    except (socket.timeout, TimeoutError):
        return ProbeResult(ip, ts, False, error_type=ErrorType.TIMEOUT, resolved_ip=ip,
                           protocol=Protocol.TCP.value)
    except OSError as exc:
        return ProbeResult(ip, ts, False, error_type=_oserror_type(exc), resolved_ip=ip,
                           protocol=Protocol.TCP.value, detail=str(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Public probe entry point
# ---------------------------------------------------------------------------


def ping(
    host: str,
    timeout_ms: int = 1000,
    ip_version: IPVersion = IPVersion.AUTO,
    implementation: str = "auto",
) -> ProbeResult:
    """One ICMP probe against `host`."""
    ip, family = resolve_host(host, ip_version)
    if ip is None:
        return ProbeResult(host, now_ts(), False, error_type=ErrorType.DNS_FAILURE)

    use_raw = implementation != "system" and (
        implementation == "socket" or RawIcmpPinger.available()
    )
    if use_raw:
        result = RawIcmpPinger.ping(ip, family, timeout_ms / 1000.0)
        if result.error_type == ErrorType.PERMISSION and implementation == "auto":
            result = system_ping(ip, family, timeout_ms)
    else:
        result = system_ping(ip, family, timeout_ms)
    result.host = host
    return result


def probe(
    host: str,
    protocol: str = Protocol.ICMP.value,
    port: int | None = None,
    timeout_ms: int = 1000,
    ip_version: IPVersion = IPVersion.AUTO,
    implementation: str = "auto",
    dns_query_host: str | None = None,
) -> ProbeResult:
    """Dispatch a probe according to the target protocol."""
    host = (host or "").strip()
    if not host:
        return ProbeResult(host, now_ts(), False, error_type=ErrorType.INVALID_TARGET)

    if protocol == Protocol.TCP.value:
        if not port:
            return ProbeResult(host, now_ts(), False, error_type=ErrorType.INVALID_TARGET,
                               protocol=protocol, detail="TCP target requires a port")
        ip, family = resolve_host(host, ip_version)
        if ip is None:
            return ProbeResult(host, now_ts(), False, error_type=ErrorType.DNS_FAILURE,
                               protocol=protocol)
        result = tcp_ping(ip, port, family, timeout_ms / 1000.0)
        result.host = host
        return result

    if protocol == Protocol.DNS.value:
        from app.network.dns import dns_probe  # local import avoids a cycle

        return dns_probe(
            resolver=host,
            query_host=dns_query_host,
            timeout_ms=timeout_ms,
            ip_version=ip_version,
        )

    return ping(host, timeout_ms, ip_version, implementation)


def ping_many(
    host: str,
    count: int = 4,
    timeout_ms: int = 1000,
    interval_s: float = 0.2,
    ip_version: IPVersion = IPVersion.AUTO,
) -> list[ProbeResult]:
    """Sequential burst of probes, used by one-off diagnostics."""
    results: list[ProbeResult] = []
    for i in range(max(1, count)):
        results.append(ping(host, timeout_ms, ip_version))
        if i < count - 1 and interval_s > 0:
            time.sleep(interval_s)
    return results
