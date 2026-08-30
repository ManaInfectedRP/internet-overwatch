"""Network adapter information (plan section 34)."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field

from app.network.gateway import detect_gateway
from app.utils.logger import get_logger
from app.utils.platform import IS_LINUX, IS_MACOS, IS_WINDOWS, run_command

log = get_logger("network.interfaces")


@dataclass(slots=True)
class InterfaceInfo:
    name: str
    ipv4: str | None = None
    ipv6: str | None = None
    mac: str | None = None
    netmask: str | None = None
    is_up: bool = False
    speed_mbps: int = 0
    mtu: int = 0
    connection_type: str = "Unknown"
    is_default: bool = False

    @property
    def link_speed_text(self) -> str:
        if self.speed_mbps <= 0:
            return "Unknown"
        if self.speed_mbps >= 1000:
            return f"{self.speed_mbps / 1000:.0f} Gbps"
        return f"{self.speed_mbps} Mbps"


@dataclass(slots=True)
class NetworkInfo:
    interfaces: list[InterfaceInfo] = field(default_factory=list)
    gateway: str | None = None
    gateway_ipv6: str | None = None
    dns_servers: list[str] = field(default_factory=list)
    default_interface: InterfaceInfo | None = None
    hostname: str = ""

    @property
    def has_ipv4(self) -> bool:
        return any(i.ipv4 for i in self.interfaces if i.is_up)

    @property
    def has_ipv6(self) -> bool:
        return any(i.ipv6 for i in self.interfaces if i.is_up)


_VIRTUAL_HINTS = ("virtual", "vmware", "hyper-v", "vethernet", "loopback", "vbox", "docker", "tap")
_WIRELESS_HINTS = ("wi-fi", "wifi", "wlan", "wireless", "airport", "wl")


def guess_connection_type(name: str) -> str:
    lowered = name.lower()
    if any(hint in lowered for hint in _WIRELESS_HINTS):
        return "Wi-Fi"
    if any(hint in lowered for hint in _VIRTUAL_HINTS):
        return "Virtual"
    if "ethernet" in lowered or lowered.startswith(("en", "eth")):
        return "Ethernet"
    return "Unknown"


def list_interfaces() -> list[InterfaceInfo]:
    """Enumerate adapters via psutil, enriched with speed and status."""
    interfaces: list[InterfaceInfo] = []
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return interfaces

    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not enumerate interfaces: %s", exc)
        return interfaces

    for name, entries in addrs.items():
        info = InterfaceInfo(name=name, connection_type=guess_connection_type(name))
        for entry in entries:
            if entry.family == socket.AF_INET and not info.ipv4:
                info.ipv4 = entry.address
                info.netmask = entry.netmask
            elif entry.family == socket.AF_INET6 and not info.ipv6:
                # Strip the zone index from link-local addresses.
                info.ipv6 = entry.address.split("%")[0]
            elif getattr(entry.family, "name", "") in ("AF_LINK", "AF_PACKET"):
                info.mac = entry.address
        stat = stats.get(name)
        if stat:
            info.is_up = stat.isup
            info.speed_mbps = int(stat.speed or 0)
            info.mtu = int(stat.mtu or 0)
        interfaces.append(info)

    interfaces.sort(key=lambda i: (not i.is_up, i.connection_type == "Virtual", i.name.lower()))
    return interfaces


def _dns_servers_windows() -> list[str]:
    servers: list[str] = []
    code, out, _ = run_command(["ipconfig", "/all"], timeout=10, encoding="utf-8")
    if code != 0:
        return servers
    in_dns_block = False
    for raw in out.splitlines():
        stripped = raw.strip()
        match = re.match(r"^(.*?)[\s.]*:\s*(\S+)?\s*$", stripped)
        label = match.group(1).strip().lower() if match else ""
        value = (match.group(2) or "").strip() if match else ""
        if "dns" in label and ("server" in label or "servrar" in label or "serveur" in label):
            in_dns_block = True
            if value:
                servers.append(value)
            continue
        if in_dns_block and stripped and not match:
            servers.append(stripped)
            continue
        if in_dns_block and stripped and match and not label:
            servers.append(value)
            continue
        if match and label:
            in_dns_block = False
    return _dedupe_ips(servers)


def _dns_servers_unix() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip().startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        servers.append(parts[1])
    except OSError:
        pass
    if not servers and IS_MACOS:
        code, out, _ = run_command(["scutil", "--dns"], timeout=5)
        if code == 0:
            servers = re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", out)
    return _dedupe_ips(servers)


def _dedupe_ips(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        try:
            import ipaddress

            ipaddress.ip_address(value)
        except ValueError:
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def detect_dns_servers() -> list[str]:
    """System DNS resolvers (plan section 34)."""
    try:
        if IS_WINDOWS:
            return _dns_servers_windows()
        if IS_LINUX or IS_MACOS:
            return _dns_servers_unix()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("DNS server detection failed: %s", exc)
    return []


def collect_network_info() -> NetworkInfo:
    """Full adapter / gateway / DNS snapshot for the Diagnostics page."""
    gateway = detect_gateway()
    interfaces = list_interfaces()
    info = NetworkInfo(
        interfaces=interfaces,
        gateway=gateway.address,
        gateway_ipv6=gateway.ipv6_address,
        dns_servers=detect_dns_servers(),
        hostname=socket.gethostname(),
    )
    if gateway.interface:
        for iface in interfaces:
            if iface.name == gateway.interface:
                iface.is_default = True
                info.default_interface = iface
                break
    if info.default_interface is None:
        for iface in interfaces:
            if iface.is_up and iface.ipv4 and iface.connection_type != "Virtual":
                iface.is_default = True
                info.default_interface = iface
                break
    return info


def get_public_ip(timeout_s: float = 3.0) -> str | None:
    """Optional public-IP lookup.

    Only called when the user explicitly asks for it (plan section 62 keeps the
    app local-first), and a failure is simply reported as unknown.
    """
    import urllib.error
    import urllib.request

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
            import ipaddress

            ipaddress.ip_address(text)
            return text
        except (urllib.error.URLError, ValueError, OSError):
            continue
    return None
