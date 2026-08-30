"""Wi-Fi radio information (plan section 33).

Read-only, best effort, and never fatal: on a wired machine, an unsupported
platform, or a locked-down OS the result is simply "not available".

Signal strength is reported as information, never as proof of a latency
problem - the plan is explicit about that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config.defaults import WIFI_SIGNAL_PROBLEM_PCT, WIFI_SIGNAL_WARNING_PCT, NodeStatus
from app.utils.logger import get_logger
from app.utils.platform import IS_LINUX, IS_MACOS, IS_WINDOWS, has_command, run_command

log = get_logger("network.wifi")


@dataclass(slots=True)
class WifiInfo:
    available: bool = False
    connected: bool = False
    ssid: str | None = None
    bssid: str | None = None
    signal_percent: int | None = None
    signal_dbm: int | None = None
    channel: str | None = None
    band: str | None = None
    radio_type: str | None = None
    receive_rate_mbps: float | None = None
    transmit_rate_mbps: float | None = None
    authentication: str | None = None
    interface: str | None = None
    note: str = ""

    @property
    def status(self) -> NodeStatus:
        if not self.available or not self.connected or self.signal_percent is None:
            return NodeStatus.UNKNOWN
        if self.signal_percent < WIFI_SIGNAL_PROBLEM_PCT:
            return NodeStatus.PROBLEM
        if self.signal_percent < WIFI_SIGNAL_WARNING_PCT:
            return NodeStatus.WARNING
        return NodeStatus.HEALTHY

    @property
    def warnings(self) -> list[str]:
        messages: list[str] = []
        if not self.connected or self.signal_percent is None:
            return messages
        if self.signal_percent < WIFI_SIGNAL_PROBLEM_PCT:
            messages.append(
                "Weak Wi-Fi signal. This can cause instability, but signal "
                "strength alone does not prove a latency problem."
            )
        elif self.signal_percent < WIFI_SIGNAL_WARNING_PCT:
            messages.append("Wi-Fi signal is moderate; wireless instability is possible.")
        if self.band == "2.4 GHz":
            messages.append("2.4 GHz is more prone to interference than 5 GHz.")
        return messages

    @property
    def link_speed_text(self) -> str:
        rates = [r for r in (self.receive_rate_mbps, self.transmit_rate_mbps) if r]
        if not rates:
            return "Unknown"
        return f"{max(rates):.0f} Mbps"


def _dbm_to_percent(dbm: float) -> int:
    """Map dBm onto the same 0-100 scale Windows reports."""
    if dbm <= -100:
        return 0
    if dbm >= -50:
        return 100
    return int(round(2 * (dbm + 100)))


def _band_from_channel(channel: str | None) -> str | None:
    if not channel:
        return None
    try:
        number = int(re.sub(r"\D", "", channel) or 0)
    except ValueError:
        return None
    if 1 <= number <= 14:
        return "2.4 GHz"
    if 32 <= number <= 177:
        return "5 GHz"
    if number >= 180:
        return "6 GHz"
    return None


def _parse_netsh(output: str) -> WifiInfo:
    """Parse `netsh wlan show interfaces`.

    Windows localises the labels, so values are matched positionally within
    each `label : value` line using tolerant keyword matching.
    """
    info = WifiInfo(available=True)
    for raw in output.splitlines():
        if ":" not in raw:
            continue
        label, _, value = raw.partition(":")
        label = label.strip().lower()
        value = value.strip()
        if not value:
            continue

        if label.startswith("name") or "granssnittsnamn" in label:
            info.interface = info.interface or value
        elif label == "ssid" or label.endswith(" ssid"):
            if "bssid" in label:
                info.bssid = value
            else:
                info.ssid = value
        elif "bssid" in label:
            info.bssid = value
        elif label.startswith("state") or label.startswith("tillstand") or label.startswith("status"):
            info.connected = "connect" in value.lower() or "anslut" in value.lower()
        elif "signal" in label:
            match = re.search(r"(\d+)", value)
            if match:
                info.signal_percent = int(match.group(1))
        elif label.startswith("channel") or "kanal" in label:
            info.channel = value
        elif "radio type" in label or "radiotyp" in label:
            info.radio_type = value
        elif "receive rate" in label or "mottagningshastighet" in label:
            info.receive_rate_mbps = _first_float(value)
        elif "transmit rate" in label or "sandningshastighet" in label:
            info.transmit_rate_mbps = _first_float(value)
        elif label.startswith("authentication") or "autentisering" in label:
            info.authentication = value
        elif label.startswith("band"):
            info.band = value
    if not info.band:
        info.band = _band_from_channel(info.channel)
    return info


def _first_float(text: str) -> float | None:
    match = re.search(r"([\d.,]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _windows_wifi() -> WifiInfo:
    code, out, err = run_command(["netsh", "wlan", "show", "interfaces"], timeout=8)
    if code != 0:
        text = (out + err).lower()
        if "not running" in text or "wireless" in text:
            return WifiInfo(available=False, note="No wireless service or adapter")
        return WifiInfo(available=False, note="Wi-Fi information unavailable")
    if "there is no wireless interface" in out.lower():
        return WifiInfo(available=False, note="No wireless adapter present")
    return _parse_netsh(out)


def _linux_wifi() -> WifiInfo:
    if has_command("nmcli"):
        code, out, _ = run_command(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,BSSID,SIGNAL,CHAN,RATE", "dev", "wifi"], timeout=6
        )
        if code == 0:
            for line in out.splitlines():
                fields = line.split(":")
                if fields and fields[0] == "yes":
                    info = WifiInfo(available=True, connected=True)
                    if len(fields) > 1:
                        info.ssid = fields[1] or None
                    if len(fields) > 2:
                        info.bssid = fields[2].replace("\\:", ":") or None
                    if len(fields) > 3 and fields[3].isdigit():
                        info.signal_percent = int(fields[3])
                    if len(fields) > 4:
                        info.channel = fields[4] or None
                    if len(fields) > 5:
                        info.receive_rate_mbps = _first_float(fields[5])
                    info.band = _band_from_channel(info.channel)
                    return info
    if has_command("iwconfig"):
        code, out, _ = run_command(["iwconfig"], timeout=6)
        if code == 0 and "ESSID" in out:
            info = WifiInfo(available=True, connected=True)
            ssid = re.search(r'ESSID:"([^"]*)"', out)
            if ssid:
                info.ssid = ssid.group(1) or None
            ap = re.search(r"Access Point:\s*([0-9A-Fa-f:]{17})", out)
            if ap:
                info.bssid = ap.group(1)
            level = re.search(r"Signal level=(-?\d+)\s*dBm", out)
            if level:
                info.signal_dbm = int(level.group(1))
                info.signal_percent = _dbm_to_percent(info.signal_dbm)
            rate = re.search(r"Bit Rate[=:]\s*([\d.]+)\s*Mb/s", out)
            if rate:
                info.receive_rate_mbps = float(rate.group(1))
            return info
    return WifiInfo(available=False, note="No Wi-Fi tooling found (nmcli / iwconfig)")


def _macos_wifi() -> WifiInfo:
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/"
        "Current/Resources/airport"
    )
    code, out, _ = run_command([airport, "-I"], timeout=6)
    if code != 0:
        return WifiInfo(available=False, note="Wi-Fi information unavailable")
    info = WifiInfo(available=True, connected=True)
    for raw in out.splitlines():
        if ":" not in raw:
            continue
        label, _, value = raw.partition(":")
        label = label.strip().lower()
        value = value.strip()
        if label == "ssid":
            info.ssid = value
        elif label == "bssid":
            info.bssid = value
        elif label == "agrctlrssi":
            try:
                info.signal_dbm = int(value)
                info.signal_percent = _dbm_to_percent(info.signal_dbm)
            except ValueError:
                pass
        elif label == "channel":
            info.channel = value
        elif label == "lastturate":
            info.receive_rate_mbps = _first_float(value)
    info.band = _band_from_channel(info.channel)
    return info


def get_wifi_info() -> WifiInfo:
    """Current Wi-Fi association, or an "unavailable" result."""
    try:
        if IS_WINDOWS:
            return _windows_wifi()
        if IS_LINUX:
            return _linux_wifi()
        if IS_MACOS:
            return _macos_wifi()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Wi-Fi lookup failed: %s", exc)
    return WifiInfo(available=False, note="Wi-Fi information not supported on this platform")
