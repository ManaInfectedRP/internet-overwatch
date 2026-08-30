"""Default gateway detection (plan section 58).

The gateway address is never hardcoded - it is read from the OS routing table.
Several strategies are tried in order and the first that yields a usable
address wins, so the app works on odd subnets and on all three platforms.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

from app.utils.logger import get_logger
from app.utils.platform import IS_LINUX, IS_MACOS, IS_WINDOWS, has_command, run_command

log = get_logger("network.gateway")


@dataclass(slots=True)
class GatewayInfo:
    address: str | None = None
    interface: str | None = None
    source: str = ""
    ipv6_address: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.address)


def _valid_gateway(candidate: str | None) -> str | None:
    if not candidate:
        return None
    candidate = candidate.strip()
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if addr.is_unspecified or addr.is_loopback:
        return None
    return candidate


def _local_ip() -> str | None:
    """Local address of the interface used for outbound traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this only selects a route.
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def interface_name_for_ip(ip: str | None) -> str | None:
    """Map a local IPv4 address to its adapter name."""
    if not ip:
        return None
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == ip:
                    return name
    except Exception:  # pragma: no cover - defensive
        pass
    return None


def _detect_psutil() -> GatewayInfo:
    """Cross-platform hint: pair the outbound local IP with its interface."""
    name = interface_name_for_ip(_local_ip())
    if name:
        return GatewayInfo(interface=name, source="psutil")
    return GatewayInfo()


def _detect_windows_route() -> GatewayInfo:
    code, out, _ = run_command(["route", "print", "-4"], timeout=8, encoding="utf-8")
    if code != 0 or not out:
        return GatewayInfo()
    for line in out.splitlines():
        parts = line.split()
        # 0.0.0.0  0.0.0.0  <gateway>  <interface>  <metric>
        if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw = _valid_gateway(parts[2])
            if gw:
                return GatewayInfo(address=gw, interface=parts[3], source="route print")
    return GatewayInfo()


def _detect_windows_ipconfig() -> GatewayInfo:
    code, out, _ = run_command(["ipconfig"], timeout=8, encoding="utf-8")
    if code != 0 or not out:
        return GatewayInfo()
    current_adapter = None
    ipv6 = None
    for raw in out.splitlines():
        line = raw.rstrip()
        if line and not line.startswith(" "):
            current_adapter = line.strip().rstrip(":")
            continue
        # Match the localised "Default Gateway" line by its structure rather
        # than its wording: a label followed by dots and an address.
        match = re.match(r"\s*([^.]+?)[\s.]*:\s*(\S+)?\s*$", line)
        if not match:
            continue
        label, value = match.group(1).strip().lower(), (match.group(2) or "").strip()
        if not value:
            continue
        looks_like_gateway = any(
            token in label
            for token in ("gateway", "gateway", "standardgateway", "passerelle", "vagg")
        )
        if looks_like_gateway or label.endswith("gateway"):
            gw = _valid_gateway(value)
            if gw and ":" not in gw:
                return GatewayInfo(address=gw, interface=current_adapter,
                                   source="ipconfig", ipv6_address=ipv6)
            if gw and ":" in gw:
                ipv6 = gw
    return GatewayInfo(ipv6_address=ipv6)


def _detect_windows_netsh() -> GatewayInfo:
    code, out, _ = run_command(
        ["netsh", "interface", "ipv4", "show", "route"], timeout=8, encoding="utf-8"
    )
    if code != 0 or not out:
        return GatewayInfo()
    for line in out.splitlines():
        if "0.0.0.0/0" not in line:
            continue
        for token in line.split():
            gw = _valid_gateway(token)
            if gw and gw != "0.0.0.0":
                return GatewayInfo(address=gw, source="netsh")
    return GatewayInfo()


def _detect_linux_ip_route() -> GatewayInfo:
    if not has_command("ip"):
        return GatewayInfo()
    code, out, _ = run_command(["ip", "route", "show", "default"], timeout=5)
    if code != 0 or not out:
        return GatewayInfo()
    match = re.search(r"default\s+via\s+(\S+)(?:\s+dev\s+(\S+))?", out)
    if match:
        gw = _valid_gateway(match.group(1))
        if gw:
            return GatewayInfo(address=gw, interface=match.group(2), source="ip route")
    return GatewayInfo()


def _detect_proc_net_route() -> GatewayInfo:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                raw = int(fields[2], 16)
                gw = socket.inet_ntoa(raw.to_bytes(4, "little"))
                if _valid_gateway(gw):
                    return GatewayInfo(address=gw, interface=fields[0], source="/proc/net/route")
    except (OSError, ValueError, StopIteration):
        pass
    return GatewayInfo()


def _detect_macos_route() -> GatewayInfo:
    code, out, _ = run_command(["route", "-n", "get", "default"], timeout=5)
    if code != 0 or not out:
        return GatewayInfo()
    gw = None
    iface = None
    for line in out.splitlines():
        if "gateway:" in line:
            gw = _valid_gateway(line.split(":", 1)[1])
        elif "interface:" in line:
            iface = line.split(":", 1)[1].strip()
    if gw:
        return GatewayInfo(address=gw, interface=iface, source="route get default")
    return GatewayInfo()


def _detect_guess_from_local_ip() -> GatewayInfo:
    """Last resort: assume .1 on the local /24. Clearly marked as a guess."""
    local = _local_ip()
    if not local:
        return GatewayInfo()
    try:
        network = ipaddress.ip_network(f"{local}/24", strict=False)
        candidate = str(next(network.hosts()))
    except (ValueError, StopIteration):
        return GatewayInfo()
    return GatewayInfo(address=candidate, source="guessed from local subnet")


def detect_gateway() -> GatewayInfo:
    """Detect the IPv4 default gateway. Returns an empty result if unknown."""
    if IS_WINDOWS:
        strategies = [_detect_windows_route, _detect_windows_ipconfig, _detect_windows_netsh]
    elif IS_MACOS:
        strategies = [_detect_macos_route]
    elif IS_LINUX:
        strategies = [_detect_linux_ip_route, _detect_proc_net_route]
    else:  # pragma: no cover - unknown platform
        strategies = [_detect_proc_net_route]

    ipv6_hint = None
    for strategy in strategies:
        try:
            info = strategy()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Gateway strategy %s failed: %s", strategy.__name__, exc)
            continue
        ipv6_hint = ipv6_hint or info.ipv6_address
        if info.found:
            # `route print` reports the interface as its IP address; show a name.
            if info.interface and _valid_gateway(info.interface):
                info.interface = interface_name_for_ip(info.interface) or info.interface
            if not info.interface:
                info.interface = _detect_psutil().interface
            info.ipv6_address = info.ipv6_address or ipv6_hint
            log.info("Gateway detected: %s via %s (%s)", info.address,
                     info.interface or "unknown interface", info.source)
            return info

    fallback = _detect_guess_from_local_ip()
    if fallback.found:
        fallback.ipv6_address = ipv6_hint
        log.warning("Gateway not found in routing table; guessing %s", fallback.address)
        return fallback

    log.warning("No default gateway could be detected")
    return GatewayInfo(ipv6_address=ipv6_hint)


def detect_gateway_address() -> str | None:
    return detect_gateway().address
