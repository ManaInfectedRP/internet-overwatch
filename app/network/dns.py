"""DNS monitoring (plan section 30).

A DNS probe sends a real A-record query straight to a resolver over UDP and
times the response. `dnspython` is used when available, otherwise a minimal
query is built by hand so the feature works with no extra dependency.
"""

from __future__ import annotations

import random
import socket
import struct
import time
from dataclasses import dataclass

from app.config.defaults import DEFAULT_DNS_PROBE_HOST, IPVersion, Protocol
from app.network.ping import ErrorType, ProbeResult, resolve_host
from app.utils.logger import get_logger
from app.utils.time import now_ts

log = get_logger("network.dns")

DNS_PORT = 53
QTYPE_A = 1
QTYPE_AAAA = 28


@dataclass(slots=True)
class DnsResult:
    resolver: str
    query_host: str
    success: bool
    latency_ms: float | None = None
    answers: list[str] = None  # type: ignore[assignment]
    error: str | None = None
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.answers is None:
            self.answers = []
        if not self.timestamp:
            self.timestamp = now_ts()


def _encode_name(name: str) -> bytes:
    parts = [label for label in name.strip(".").split(".") if label]
    out = bytearray()
    for label in parts:
        encoded = label.encode("idna") if not label.isascii() else label.encode("ascii")
        if len(encoded) > 63:
            raise ValueError("DNS label too long")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _build_query(name: str, qtype: int, query_id: int) -> bytes:
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)  # RD set
    question = _encode_name(name) + struct.pack("!HH", qtype, 1)
    return header + question


def _skip_name(data: bytes, offset: int) -> int:
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # compression pointer
            return offset + 2
        offset += length + 1
    return offset


def _parse_answers(data: bytes, qtype: int) -> list[str]:
    if len(data) < 12:
        return []
    _id, _flags, qdcount, ancount, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(data, offset) + 4
    answers: list[str] = []
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlength]
        offset += rdlength
        if rtype == QTYPE_A and rdlength == 4:
            answers.append(socket.inet_ntop(socket.AF_INET, rdata))
        elif rtype == QTYPE_AAAA and rdlength == 16:
            answers.append(socket.inet_ntop(socket.AF_INET6, rdata))
    return answers


def _rcode(data: bytes) -> int:
    if len(data) < 4:
        return -1
    flags = struct.unpack("!H", data[2:4])[0]
    return flags & 0x000F


def query_dns(
    resolver: str,
    query_host: str = DEFAULT_DNS_PROBE_HOST,
    timeout_ms: int = 2000,
    qtype: int = QTYPE_A,
) -> DnsResult:
    """Send one A query to `resolver` and time the response."""
    resolver = (resolver or "").strip()
    query_host = (query_host or DEFAULT_DNS_PROBE_HOST).strip()
    if not resolver:
        return DnsResult(resolver, query_host, False, error=ErrorType.INVALID_TARGET)

    resolver_ip, family = resolve_host(resolver)
    if resolver_ip is None:
        return DnsResult(resolver, query_host, False, error=ErrorType.DNS_FAILURE)

    query_id = random.randint(0, 0xFFFF)
    try:
        packet = _build_query(query_host, qtype, query_id)
    except (ValueError, UnicodeError):
        return DnsResult(resolver, query_host, False, error=ErrorType.INVALID_TARGET)

    sock = socket.socket(family or socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(max(0.1, timeout_ms / 1000.0))
    start = time.perf_counter()
    try:
        sock.sendto(packet, (resolver_ip, DNS_PORT))
        while True:
            data, _addr = sock.recvfrom(4096)
            if len(data) >= 2 and struct.unpack("!H", data[:2])[0] != query_id:
                continue  # stale or spoofed reply, keep waiting
            elapsed = (time.perf_counter() - start) * 1000.0
            rcode = _rcode(data)
            if rcode not in (0, 3):  # 3 == NXDOMAIN, still a working resolver
                return DnsResult(resolver, query_host, False, latency_ms=elapsed,
                                 error=f"rcode {rcode}")
            return DnsResult(resolver, query_host, True, latency_ms=elapsed,
                             answers=_parse_answers(data, qtype))
    except (socket.timeout, TimeoutError):
        return DnsResult(resolver, query_host, False, error=ErrorType.TIMEOUT)
    except OSError as exc:
        return DnsResult(resolver, query_host, False, error=str(exc))
    finally:
        sock.close()


def dns_probe(
    resolver: str,
    query_host: str | None = None,
    timeout_ms: int = 2000,
    ip_version: IPVersion = IPVersion.AUTO,
) -> ProbeResult:
    """DNS probe shaped like a latency measurement, for monitored targets."""
    qtype = QTYPE_AAAA if ip_version == IPVersion.IPV6 else QTYPE_A
    result = query_dns(resolver, query_host or DEFAULT_DNS_PROBE_HOST, timeout_ms, qtype)
    return ProbeResult(
        host=resolver,
        timestamp=result.timestamp,
        success=result.success,
        latency_ms=result.latency_ms,
        error_type=None if result.success else (result.error or ErrorType.UNKNOWN),
        protocol=Protocol.DNS.value,
        resolved_ip=resolver,
        detail=f"query {result.query_host}",
    )


@dataclass(slots=True)
class DnsHealth:
    resolver: str
    latency_ms: float | None
    failures: int
    samples: int

    @property
    def loss_fraction(self) -> float:
        return self.failures / self.samples if self.samples else 0.0

    @property
    def healthy(self) -> bool:
        return self.failures == 0 and self.latency_ms is not None


def check_resolvers(
    resolvers: list[str],
    query_host: str = DEFAULT_DNS_PROBE_HOST,
    samples: int = 3,
    timeout_ms: int = 2000,
) -> list[DnsHealth]:
    """Measure each resolver a few times; used by the Diagnostics page."""
    health: list[DnsHealth] = []
    for resolver in resolvers:
        latencies: list[float] = []
        failures = 0
        for _ in range(max(1, samples)):
            result = query_dns(resolver, query_host, timeout_ms)
            if result.success and result.latency_ms is not None:
                latencies.append(result.latency_ms)
            else:
                failures += 1
        average = sum(latencies) / len(latencies) if latencies else None
        health.append(DnsHealth(resolver, average, failures, max(1, samples)))
    return health


def system_resolution_time(host: str = DEFAULT_DNS_PROBE_HOST) -> float | None:
    """Time a resolution through the OS resolver (includes its cache)."""
    start = time.perf_counter()
    try:
        socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return None
    return (time.perf_counter() - start) * 1000.0
