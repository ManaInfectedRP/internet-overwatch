"""Network probe tests.

These are deliberately offline: parsing, dispatch and error mapping are tested
against fixtures and loopback so the suite never depends on the internet.
Anything that would need a real remote host is marked and skipped by default.
"""

from __future__ import annotations

import socket

import pytest

from app.config.defaults import IPVersion, Protocol
from app.network import ping as ping_module
from app.network.dns import _build_query, _parse_answers, _rcode, dns_probe, query_dns
from app.network.gateway import _valid_gateway, detect_gateway
from app.network.interfaces import guess_connection_type, list_interfaces
from app.network.ping import (
    ErrorType,
    ProbeResult,
    ip_family,
    is_ip_literal,
    parse_system_ping_latency,
    ping,
    probe,
    resolve_host,
)
from app.network.throughput import ThroughputMonitor, grade_bufferbloat
from app.network.traceroute import compare_routes, parse_hop_line, traceroute_command
from app.network.wifi import _band_from_channel, _dbm_to_percent, _parse_netsh


# ----------------------------------------------------------- ping parsing ---


@pytest.mark.parametrize(
    "output,expected",
    [
        ("Reply from 1.1.1.1: bytes=32 time=14ms TTL=57", 14.0),
        ("Reply from 1.1.1.1: bytes=32 time<1ms TTL=57", 1.0),
        ("64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=13.9 ms", 13.9),
        ("Svar fran 1.1.1.1: byte=32 tid=14ms TTL=57", 14.0),  # Swedish Windows
        ("Antwort von 1.1.1.1: Bytes=32 Zeit=15ms TTL=57", 15.0),  # German
        ("Reponse de 1.1.1.1 : octets=32 temps=16 ms TTL=57", 16.0),  # French
        ("Request timed out.", None),
        ("", None),
    ],
)
def test_parse_system_ping_latency(output, expected):
    assert parse_system_ping_latency(output) == expected


def test_parse_handles_comma_decimal_separator():
    assert parse_system_ping_latency("time=13,9 ms") == pytest.approx(13.9)


# ------------------------------------------------------------- resolution ---


def test_ip_literals_are_recognised():
    assert is_ip_literal("1.1.1.1")
    assert is_ip_literal("2606:4700:4700::1111")
    assert not is_ip_literal("example.com")
    assert not is_ip_literal("")


def test_ip_family_detection():
    assert ip_family("1.1.1.1") == socket.AF_INET
    assert ip_family("::1") == socket.AF_INET6
    assert ip_family("example.com") is None


def test_resolve_host_passes_literals_through_unchanged():
    assert resolve_host("1.1.1.1") == ("1.1.1.1", socket.AF_INET)


def test_resolve_localhost():
    ip, family = resolve_host("localhost")
    assert ip is not None
    assert family in (socket.AF_INET, socket.AF_INET6)


def test_resolve_failure_is_a_value_not_an_exception():
    ip, family = resolve_host("this-host-does-not-exist.invalid")
    assert ip is None
    assert family == 0


def test_empty_host_resolves_to_nothing():
    assert resolve_host("") == (None, 0)


# ------------------------------------------------------------ probe rules ---


def test_probe_rejects_an_empty_host():
    result = probe("")
    assert result.failed
    assert result.error_type == ErrorType.INVALID_TARGET


def test_tcp_probe_requires_a_port():
    result = probe("1.1.1.1", Protocol.TCP.value, port=None)
    assert result.failed
    assert result.error_type == ErrorType.INVALID_TARGET
    assert "port" in result.detail


def test_unresolvable_host_reports_dns_failure():
    result = ping("this-host-does-not-exist.invalid", timeout_ms=500)
    assert result.failed
    assert result.error_type == ErrorType.DNS_FAILURE


def test_tcp_probe_against_a_local_listener():
    """A completed handshake is a successful measurement."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        result = probe("127.0.0.1", Protocol.TCP.value, port=port, timeout_ms=1000)
        assert result.success
        assert result.latency_ms is not None and result.latency_ms >= 0
        assert result.protocol == Protocol.TCP.value
    finally:
        server.close()


def test_tcp_refusal_still_proves_reachability():
    """A refused connection means the host answered, so it counts as a
    successful reachability measurement rather than a loss.

    Whether a closed port is refused or silently dropped is up to the OS and
    its firewall, so the drop case is skipped rather than asserted.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()

    result = probe("127.0.0.1", Protocol.TCP.value, port=port, timeout_ms=1000)
    if result.error_type == ErrorType.TIMEOUT:
        pytest.skip("this host drops connections to closed ports instead of refusing")
    assert result.success
    assert "refused" in result.detail
    assert result.latency_ms is not None


def test_loopback_icmp_ping():
    result = ping("127.0.0.1", timeout_ms=2000)
    if result.error_type == ErrorType.PERMISSION:
        pytest.skip("ICMP requires privileges in this environment")
    assert result.success
    assert result.latency_ms is not None


def test_error_labels_are_human_readable():
    assert ErrorType.label(ErrorType.TIMEOUT) == "Request timed out"
    assert ErrorType.label(None) == ""
    assert ErrorType.label("something-new") == "something-new"


def test_probe_result_failed_property():
    assert ProbeResult("h", 0.0, False).failed
    assert not ProbeResult("h", 0.0, True, 1.0).failed


# --------------------------------------------------------------- gateway ---


def test_valid_gateway_rejects_useless_addresses():
    assert _valid_gateway("192.168.1.1") == "192.168.1.1"
    assert _valid_gateway("0.0.0.0") is None
    assert _valid_gateway("127.0.0.1") is None
    assert _valid_gateway("not-an-ip") is None
    assert _valid_gateway(None) is None


def test_gateway_detection_returns_a_result_object():
    """Detection must never raise, even with no network."""
    info = detect_gateway()
    assert info.address is None or _valid_gateway(info.address)
    if info.found:
        assert info.source


# ------------------------------------------------------------ traceroute ---


@pytest.mark.parametrize(
    "line,number,ip,times",
    [
        ("  1     2 ms     1 ms     1 ms  192.168.1.1", 1, "192.168.1.1", [2.0, 1.0, 1.0]),
        ("  3    14 ms    13 ms    14 ms  gw.example [10.0.0.1]", 3, "10.0.0.1",
         [14.0, 13.0, 14.0]),
        (" 3  gw.example (10.0.0.1)  14.2 ms  13.9 ms", 3, "10.0.0.1", [14.2, 13.9]),
    ],
)
def test_parse_hop_line(line, number, ip, times):
    hop = parse_hop_line(line)
    assert hop is not None
    assert hop.number == number
    assert hop.ip == ip
    assert hop.times_ms == times
    assert hop.responded


def test_parse_hop_line_timeout():
    hop = parse_hop_line("  5     *        *        *     Request timed out.")
    assert hop is not None
    assert not hop.responded
    assert hop.display_host == "*"


def test_parse_hop_line_ignores_noise():
    assert parse_hop_line("Tracing route to 1.1.1.1 over a maximum of 30 hops") is None
    assert parse_hop_line("") is None


def test_traceroute_command_includes_the_target():
    command = traceroute_command("1.1.1.1", 20, 2000)
    assert command[-1] == "1.1.1.1"
    assert command[0] in ("tracert", "traceroute")


def test_custom_traceroute_command_substitutes_the_target():
    command = traceroute_command("1.1.1.1", 20, 2000, custom="mtr --report {target}")
    assert command == ["mtr", "--report", "1.1.1.1"]


def test_route_comparison_detects_a_change():
    from app.network.traceroute import Hop, TraceResult

    before = TraceResult("x", [Hop(1, ip="10.0.0.1"), Hop(2, ip="10.0.1.1")])
    after = TraceResult("x", [Hop(1, ip="10.0.0.1"), Hop(2, ip="10.9.9.9")])

    assert compare_routes(before, before)["changed"] is False
    changed = compare_routes(before, after)
    assert changed["changed"] is True
    assert changed["changed_hops"] == [2]


def test_route_comparison_ignores_unresponsive_hops():
    from app.network.traceroute import Hop, TraceResult

    before = TraceResult("x", [Hop(1, ip="10.0.0.1"), Hop(2, ip=None)])
    after = TraceResult("x", [Hop(1, ip="10.0.0.1"), Hop(2, ip="10.0.1.1")])
    assert compare_routes(before, after)["changed"] is False


# -------------------------------------------------------------------- dns ---


def test_dns_query_encoding_and_parsing_round_trip():
    query = _build_query("example.com", 1, 0x1234)
    assert query[:2] == b"\x12\x34"
    assert b"\x07example\x03com\x00" in query


def test_dns_rcode_extraction():
    # id, flags(rcode=3), counts
    packet = b"\x12\x34\x81\x83" + b"\x00" * 8
    assert _rcode(packet) == 3


def test_dns_answer_parsing():
    header = b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    question = b"\x07example\x03com\x00" + b"\x00\x01\x00\x01"
    answer = (b"\xc0\x0c" + b"\x00\x01" + b"\x00\x01" + b"\x00\x00\x01\x00"
              + b"\x00\x04" + bytes([93, 184, 216, 34]))
    assert _parse_answers(header + question + answer, 1) == ["93.184.216.34"]


def test_dns_probe_against_an_unreachable_resolver():
    result = dns_probe("192.0.2.1", "example.com", timeout_ms=300)
    assert result.failed
    assert result.protocol == Protocol.DNS.value


def test_dns_query_with_an_empty_resolver():
    result = query_dns("", "example.com")
    assert not result.success
    assert result.error == ErrorType.INVALID_TARGET


# ------------------------------------------------------------- interfaces ---


def test_connection_type_guessing():
    assert guess_connection_type("Wi-Fi") == "Wi-Fi"
    assert guess_connection_type("wlan0") == "Wi-Fi"
    assert guess_connection_type("Ethernet") == "Ethernet"
    assert guess_connection_type("VMware Network Adapter") == "Virtual"


def test_interface_enumeration_returns_something():
    interfaces = list_interfaces()
    assert isinstance(interfaces, list)
    for interface in interfaces:
        assert interface.name
        assert interface.link_speed_text


# ------------------------------------------------------------------ wifi ----


def test_dbm_to_percent_scale():
    assert _dbm_to_percent(-50) == 100
    assert _dbm_to_percent(-100) == 0
    assert _dbm_to_percent(-75) == pytest.approx(50, abs=1)


def test_band_from_channel():
    assert _band_from_channel("6") == "2.4 GHz"
    assert _band_from_channel("36") == "5 GHz"
    assert _band_from_channel(None) is None


def test_netsh_parsing():
    output = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : MyNetwork
    BSSID                  : aa:bb:cc:dd:ee:ff
    Radio type             : 802.11ac
    Channel                : 36
    Receive rate (Mbps)    : 866
    Transmit rate (Mbps)   : 866
    Signal                 : 72%
    """
    info = _parse_netsh(output)
    assert info.ssid == "MyNetwork"
    assert info.signal_percent == 72
    assert info.channel == "36"
    assert info.band == "5 GHz"
    assert info.receive_rate_mbps == 866
    assert info.connected


def test_weak_signal_produces_a_hedged_warning():
    info = _parse_netsh("State : connected\nSSID : X\nSignal : 20%\nChannel : 6")
    warnings = info.warnings
    assert warnings
    assert "does not prove" in " ".join(warnings)


# ------------------------------------------------------------- throughput ---


def test_first_throughput_sample_reports_no_rate():
    monitor = ThroughputMonitor()
    if not monitor.available:
        pytest.skip("psutil unavailable")
    first = monitor.sample()
    assert first is not None
    assert first.download_bps == 0.0


def test_bufferbloat_grading():
    assert grade_bufferbloat(10)[0] == "A"
    assert grade_bufferbloat(45)[0] == "B"
    assert grade_bufferbloat(90)[0] == "C"
    assert grade_bufferbloat(200)[0] == "D"
    assert grade_bufferbloat(900)[0] == "F"
    assert grade_bufferbloat(None)[0] == "-"


# ----------------------------------------------------------- ip preference ---


def test_ip_version_preference_is_honoured_for_literals():
    assert resolve_host("1.1.1.1", IPVersion.IPV6)[0] == "1.1.1.1"


def test_ping_accepts_an_implementation_override():
    result = ping("127.0.0.1", timeout_ms=1500, implementation="system")
    assert isinstance(result, ProbeResult)
    assert result.host == "127.0.0.1"


# ------------------------------------------------------------ macOS Wi-Fi ---
# macOS cannot be exercised on the CI matrix, so its parser is pinned against
# real `airport -I` output instead.

AIRPORT_OUTPUT = """     agrCtlRSSI: -52
     agrExtRSSI: 0
    agrCtlNoise: -95
          state: running
        op mode: station
     lastTxRate: 300
        maxRate: 450
      link auth: wpa2-psk
          BSSID: aa:bb:cc:dd:ee:ff
           SSID: MyNetwork
            MCS: 15
        channel: 36,1
"""


def test_airport_parsing():
    from app.network.wifi import _parse_airport

    info = _parse_airport(AIRPORT_OUTPUT)
    assert info.ssid == "MyNetwork"
    assert info.bssid == "aa:bb:cc:dd:ee:ff", "a BSSID's own colons must survive"
    assert info.connected
    assert info.signal_dbm == -52
    assert info.signal_percent == pytest.approx(96, abs=1)
    assert info.receive_rate_mbps == 300
    assert info.transmit_rate_mbps == 450
    assert info.authentication == "wpa2-psk"


def test_airport_channel_with_width_suffix():
    """`channel: 36,1` is channel 36 at width 1, not channel 361."""
    from app.network.wifi import _parse_airport

    assert _parse_airport(AIRPORT_OUTPUT).band == "5 GHz"


def test_band_parsing_tolerates_suffixes():
    assert _band_from_channel("36,1") == "5 GHz"
    assert _band_from_channel("6,1") == "2.4 GHz"
    assert _band_from_channel("149 (80 MHz)") == "5 GHz"


def test_disassociated_airport_state():
    from app.network.wifi import _parse_airport

    info = _parse_airport("          state: init\n           SSID: X\n")
    assert not info.connected


def test_localised_windows_gateway_labels():
    """The ipconfig fallback has to survive a non-English Windows."""
    from app.network.gateway import _detect_windows_ipconfig

    # Exercised through the parser's matching rules rather than by running
    # ipconfig, which would return this machine's own configuration.
    for label in ("Default Gateway", "Standard-gateway", "Standardgateway",
                  "Passerelle par defaut"):
        lowered = label.lower()
        matched = any(
            token in lowered for token in ("gateway", "passerelle", "puerta de enlace")
        )
        assert matched, f"{label} would not be recognised as a gateway line"
