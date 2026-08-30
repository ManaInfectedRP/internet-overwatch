"""Synthetic measurement generator (plan sections 65, 66).

Lets the dashboard, detector and diagnosis rules be exercised without waiting
for the network to misbehave. Each scenario produces a deterministic-ish stream
of measurements across router / internet / game targets, including the failure
modes from plan section 64.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from app.config.defaults import TargetCategory
from app.network.ping import ErrorType
from app.storage.models import Measurement, Target
from app.utils.logger import get_logger
from app.utils.time import now_ts

log = get_logger("core.simulator")


SCENARIOS = [
    "stable",
    "high_latency",
    "jitter",
    "packet_loss",
    "router_spikes",
    "isp_spikes",
    "destination_spikes",
    "complete_outage",
    "bufferbloat_pattern",
]

SCENARIO_LABELS = {
    "stable": "Stable connection",
    "high_latency": "Consistently high latency",
    "jitter": "High jitter",
    "packet_loss": "Packet loss",
    "router_spikes": "Local router spikes",
    "isp_spikes": "ISP / upstream spikes",
    "destination_spikes": "Destination-only spikes",
    "complete_outage": "Complete outage",
    "bufferbloat_pattern": "Latency under load (bufferbloat)",
}

SCENARIO_DESCRIPTIONS = {
    "stable": "Everything healthy - the baseline case.",
    "high_latency": "All layers slow but steady; no spikes.",
    "jitter": "Latency swings sample to sample without large peaks.",
    "packet_loss": "Regular dropped probes on the internet and game targets.",
    "router_spikes": "Router and everything behind it spike together - looks local.",
    "isp_spikes": "Public targets spike together while the router stays clean.",
    "destination_spikes": "Only the game target spikes; router and internet stay clean.",
    "complete_outage": "Nothing responds, then recovery.",
    "bufferbloat_pattern": "Latency rises in sustained waves, as under saturation.",
}


@dataclass
class SimulatedTarget:
    target: Target
    baseline_ms: float
    noise_ms: float = 2.0


@dataclass
class Simulator:
    """Generates measurements for a scenario.

    `step()` advances virtual time by one tick and returns one measurement per
    target, so tests can run thousands of ticks instantly while the UI mode
    feeds them in real time.
    """

    scenario: str = "stable"
    targets: list[SimulatedTarget] = field(default_factory=list)
    tick_s: float = 0.5
    seed: int | None = None

    def __post_init__(self) -> None:
        self.random = random.Random(self.seed)
        self.tick = 0
        self.start_time = now_ts()
        if not self.targets:
            self.targets = self.default_targets()

    @staticmethod
    def default_targets() -> list[SimulatedTarget]:
        return [
            SimulatedTarget(
                Target(id=-1, name="Router (sim)", host="192.168.0.1",
                       category=TargetCategory.GATEWAY.value, interval_ms=250),
                baseline_ms=2.0, noise_ms=0.6,
            ),
            SimulatedTarget(
                Target(id=-2, name="Cloudflare (sim)", host="1.1.1.1",
                       category=TargetCategory.INTERNET.value, interval_ms=500),
                baseline_ms=21.0, noise_ms=1.5,
            ),
            SimulatedTarget(
                Target(id=-3, name="Google DNS (sim)", host="8.8.8.8",
                       category=TargetCategory.INTERNET.value, interval_ms=500),
                baseline_ms=24.0, noise_ms=1.8,
            ),
            SimulatedTarget(
                Target(id=-4, name="Game Server (sim)", host="game.example",
                       category=TargetCategory.CUSTOM.value, interval_ms=500),
                baseline_ms=81.0, noise_ms=3.0,
            ),
        ]

    # ------------------------------------------------------------ scenarios ---
    def _spike_active(self, period: int, length: int) -> bool:
        return (self.tick % period) < length

    def _value_for(self, sim: SimulatedTarget) -> tuple[bool, float | None, str | None]:
        """Return (success, latency, error) for one target at this tick."""
        category = sim.target.category
        base = sim.baseline_ms
        noise = self.random.gauss(0, sim.noise_ms)
        latency = max(0.3, base + noise)
        scenario = self.scenario

        if scenario == "stable":
            return True, latency, None

        if scenario == "high_latency":
            multiplier = 1.0 if category == TargetCategory.GATEWAY.value else 3.2
            return True, latency * multiplier, None

        if scenario == "jitter":
            swing = 0.0 if category == TargetCategory.GATEWAY.value else self.random.uniform(-1, 1) * 45
            return True, max(1.0, latency + swing), None

        if scenario == "packet_loss":
            if category != TargetCategory.GATEWAY.value and self.random.random() < 0.08:
                return False, None, ErrorType.TIMEOUT
            return True, latency, None

        if scenario == "router_spikes":
            # Local congestion: the router spikes and everything inherits it.
            if self._spike_active(40, 5):
                added = self.random.uniform(180, 320)
                return True, latency + added, None
            return True, latency, None

        if scenario == "isp_spikes":
            if category == TargetCategory.GATEWAY.value:
                return True, latency, None
            if self._spike_active(46, 4):
                return True, latency + self.random.uniform(150, 400), None
            return True, latency, None

        if scenario == "destination_spikes":
            if category != TargetCategory.CUSTOM.value:
                return True, latency, None
            if self._spike_active(34, 4):
                return True, latency + self.random.uniform(220, 430), None
            return True, latency, None

        if scenario == "complete_outage":
            # 20 ticks down, then recovery.
            if 20 <= self.tick % 60 < 40:
                return False, None, ErrorType.TIMEOUT
            return True, latency, None

        if scenario == "bufferbloat_pattern":
            wave = (math.sin(self.tick / 9.0) + 1) / 2  # 0..1
            if category == TargetCategory.GATEWAY.value:
                return True, latency + wave * 4, None
            return True, latency + wave * 260, None

        return True, latency, None

    # ----------------------------------------------------------------- run ---
    def step(self, timestamp: float | None = None) -> list[Measurement]:
        """Advance one tick and produce one measurement per target."""
        timestamp = timestamp if timestamp is not None else (
            self.start_time + self.tick * self.tick_s
        )
        measurements: list[Measurement] = []
        for sim in self.targets:
            success, latency, error = self._value_for(sim)
            measurements.append(
                Measurement(
                    target_id=sim.target.id,
                    target_name=sim.target.name,
                    category=sim.target.category,
                    timestamp=timestamp,
                    success=success,
                    latency_ms=latency,
                    error_type=error,
                )
            )
        self.tick += 1
        return measurements

    def stream(self, ticks: int) -> Iterator[Measurement]:
        for _ in range(ticks):
            yield from self.step()

    def target_models(self) -> list[Target]:
        return [sim.target for sim in self.targets]


class SimulationRunner(threading.Thread):
    """Feeds a :class:`Simulator` into a monitor in real time."""

    def __init__(self, simulator: Simulator, sink: Callable[[Measurement], None]) -> None:
        super().__init__(name="simulator", daemon=True)
        self.simulator = simulator
        self.sink = sink
        self.stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing loop
        log.info("Simulation started: %s", self.simulator.scenario)
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            for measurement in self.simulator.step(now_ts()):
                try:
                    self.sink(measurement)
                except Exception as exc:
                    log.warning("Simulation sink failed: %s", exc)
            next_tick += self.simulator.tick_s
            if self.stop_event.wait(max(0.0, next_tick - time.monotonic())):
                break
        log.info("Simulation stopped")

    def stop(self) -> None:
        self.stop_event.set()
