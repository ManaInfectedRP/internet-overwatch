"""Passive throughput sampling and the manual bufferbloat test.

Throughput is read from the OS interface counters via psutil (plan section 31) -
no traffic is generated for it. Speed tests and the bufferbloat test are manual
because they would themselves cause lag if run while gaming (plan section 32).
"""

from __future__ import annotations

import statistics
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from app.config.defaults import BUFFERBLOAT_BANDS
from app.utils.logger import get_logger
from app.utils.time import now_ts

log = get_logger("network.throughput")


@dataclass(slots=True)
class ThroughputSample:
    timestamp: float
    download_bps: float
    upload_bps: float
    bytes_recv: int = 0
    bytes_sent: int = 0
    cpu_percent: float = 0.0
    memory_percent: float = 0.0


class ThroughputMonitor:
    """Turns cumulative interface byte counters into a bit-rate.

    The first sample only establishes a baseline and reports zero, which is
    correct: no interval has elapsed yet.
    """

    def __init__(self, interface: str | None = None, sample_system: bool = True) -> None:
        self.interface = interface
        self.sample_system = sample_system
        self._last_counters: tuple[float, int, int] | None = None
        self._lock = threading.Lock()
        try:
            import psutil  # noqa: F401

            self._available = True
        except ImportError:  # pragma: no cover
            self._available = False
            log.warning("psutil unavailable - throughput monitoring disabled")

    @property
    def available(self) -> bool:
        return self._available

    def _read_counters(self) -> tuple[int, int] | None:
        try:
            import psutil

            if self.interface:
                per_nic = psutil.net_io_counters(pernic=True)
                counters = per_nic.get(self.interface)
                if counters is None:
                    counters = psutil.net_io_counters()
            else:
                counters = psutil.net_io_counters()
            if counters is None:
                return None
            return int(counters.bytes_recv), int(counters.bytes_sent)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Could not read network counters: %s", exc)
            return None

    def sample(self) -> ThroughputSample | None:
        """Return the rate since the previous call."""
        if not self._available:
            return None
        counters = self._read_counters()
        if counters is None:
            return None
        recv, sent = counters
        now = time.monotonic()

        with self._lock:
            previous = self._last_counters
            self._last_counters = (now, recv, sent)

        download_bps = upload_bps = 0.0
        if previous is not None:
            elapsed = now - previous[0]
            if elapsed > 0:
                # Counters can wrap or reset when an adapter reconnects.
                delta_recv = max(0, recv - previous[1])
                delta_sent = max(0, sent - previous[2])
                download_bps = delta_recv * 8.0 / elapsed
                upload_bps = delta_sent * 8.0 / elapsed

        cpu = memory = 0.0
        if self.sample_system:
            try:
                import psutil

                cpu = psutil.cpu_percent(interval=None)
                memory = psutil.virtual_memory().percent
            except Exception:  # pragma: no cover - defensive
                pass

        return ThroughputSample(
            timestamp=now_ts(),
            download_bps=download_bps,
            upload_bps=upload_bps,
            bytes_recv=recv,
            bytes_sent=sent,
            cpu_percent=cpu,
            memory_percent=memory,
        )

    def reset(self) -> None:
        with self._lock:
            self._last_counters = None


# ---------------------------------------------------------------------------
# Bufferbloat test (plan section 32)
# ---------------------------------------------------------------------------

# Public test files used only when the user starts the test explicitly.
DEFAULT_DOWNLOAD_URLS = [
    "https://speed.cloudflare.com/__down?bytes=25000000",
    "http://speedtest.tele2.net/10MB.zip",
]
DEFAULT_UPLOAD_URL = "https://speed.cloudflare.com/__up"


@dataclass(slots=True)
class BufferbloatResult:
    idle_latency_ms: float | None = None
    download_latency_ms: float | None = None
    upload_latency_ms: float | None = None
    download_mbps: float = 0.0
    upload_mbps: float = 0.0
    grade: str = "-"
    verdict: str = ""
    error: str | None = None
    samples: dict[str, list[float]] = field(default_factory=dict)

    @property
    def download_increase_ms(self) -> float | None:
        if self.idle_latency_ms is None or self.download_latency_ms is None:
            return None
        return self.download_latency_ms - self.idle_latency_ms

    @property
    def upload_increase_ms(self) -> float | None:
        if self.idle_latency_ms is None or self.upload_latency_ms is None:
            return None
        return self.upload_latency_ms - self.idle_latency_ms

    @property
    def worst_increase_ms(self) -> float | None:
        increases = [v for v in (self.download_increase_ms, self.upload_increase_ms)
                     if v is not None]
        return max(increases) if increases else None


def grade_bufferbloat(increase_ms: float | None) -> tuple[str, str]:
    if increase_ms is None:
        return "-", "Not measured"
    for limit, grade, verdict in BUFFERBLOAT_BANDS:
        if increase_ms < limit:
            return grade, verdict
    return "F", "Severe bufferbloat"  # pragma: no cover - covered by inf band


class BufferbloatTest:
    """Measures latency while the link is saturated, in three phases."""

    def __init__(
        self,
        ping_host: str,
        timeout_ms: int = 1000,
        duration_s: float = 8.0,
        download_urls: list[str] | None = None,
        upload_url: str = DEFAULT_UPLOAD_URL,
    ) -> None:
        self.ping_host = ping_host
        self.timeout_ms = timeout_ms
        self.duration_s = max(3.0, duration_s)
        self.download_urls = download_urls or list(DEFAULT_DOWNLOAD_URLS)
        self.upload_url = upload_url
        self.stop_event = threading.Event()

    # -- latency sampling -------------------------------------------------
    def _sample_latency(self, duration_s: float) -> list[float]:
        from app.network.ping import ping

        samples: list[float] = []
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline and not self.stop_event.is_set():
            result = ping(self.ping_host, self.timeout_ms)
            if result.success and result.latency_ms is not None:
                samples.append(result.latency_ms)
            time.sleep(0.15)
        return samples

    # -- load generators --------------------------------------------------
    def _saturate_download(self, duration_s: float, stats: dict) -> None:
        total = 0
        start = time.monotonic()
        for url in self.download_urls:
            if self.stop_event.is_set() or time.monotonic() - start >= duration_s:
                break
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "InternetOverwatch"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    while time.monotonic() - start < duration_s and not self.stop_event.is_set():
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.debug("Download load from %s failed: %s", url, exc)
                continue
        elapsed = max(0.001, time.monotonic() - start)
        stats["bytes"] = total
        stats["mbps"] = total * 8 / elapsed / 1e6

    def _saturate_upload(self, duration_s: float, stats: dict) -> None:
        total = 0
        start = time.monotonic()
        payload = b"0" * 512_000
        while time.monotonic() - start < duration_s and not self.stop_event.is_set():
            try:
                request = urllib.request.Request(
                    self.upload_url,
                    data=payload,
                    headers={"Content-Type": "application/octet-stream",
                             "User-Agent": "InternetOverwatch"},
                )
                with urllib.request.urlopen(request, timeout=10):
                    total += len(payload)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.debug("Upload load failed: %s", exc)
                break
        elapsed = max(0.001, time.monotonic() - start)
        stats["bytes"] = total
        stats["mbps"] = total * 8 / elapsed / 1e6

    # -- test -------------------------------------------------------------
    def run(self, progress: Callable[[str, float], None] | None = None) -> BufferbloatResult:
        """Run idle / download / upload phases and grade the result."""
        result = BufferbloatResult()

        def report(phase: str, fraction: float) -> None:
            if progress is not None:
                try:
                    progress(phase, fraction)
                except Exception:  # pragma: no cover - UI callback safety
                    pass

        phase_duration = self.duration_s

        report("Measuring idle latency", 0.05)
        idle = self._sample_latency(phase_duration * 0.6)
        result.samples["idle"] = idle
        if not idle:
            result.error = "Could not measure idle latency - is the target reachable?"
            return result
        result.idle_latency_ms = statistics.median(idle)

        report("Saturating download", 0.35)
        dl_stats: dict = {}
        thread = threading.Thread(target=self._saturate_download,
                                  args=(phase_duration, dl_stats), daemon=True)
        thread.start()
        time.sleep(0.5)  # let the transfer ramp up before sampling
        loaded = self._sample_latency(phase_duration - 0.5)
        thread.join(timeout=5)
        result.samples["download"] = loaded
        if loaded:
            result.download_latency_ms = statistics.median(loaded)
        result.download_mbps = float(dl_stats.get("mbps", 0.0))

        report("Recovering", 0.65)
        time.sleep(1.5)

        report("Saturating upload", 0.75)
        ul_stats: dict = {}
        thread = threading.Thread(target=self._saturate_upload,
                                  args=(phase_duration, ul_stats), daemon=True)
        thread.start()
        time.sleep(0.5)
        loaded_up = self._sample_latency(phase_duration - 0.5)
        thread.join(timeout=5)
        result.samples["upload"] = loaded_up
        if loaded_up:
            result.upload_latency_ms = statistics.median(loaded_up)
        result.upload_mbps = float(ul_stats.get("mbps", 0.0))

        result.grade, result.verdict = grade_bufferbloat(result.worst_increase_ms)
        report("Done", 1.0)
        log.info(
            "Bufferbloat test: idle=%.1fms download=%.1fms upload=%.1fms grade=%s",
            result.idle_latency_ms or 0.0,
            result.download_latency_ms or 0.0,
            result.upload_latency_ms or 0.0,
            result.grade,
        )
        return result

    def cancel(self) -> None:
        self.stop_event.set()
