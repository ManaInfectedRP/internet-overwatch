<p align="center">
  <img src="assets/app_logo.png" alt="Internet Overwatch" width="180">
</p>

<h1 align="center">Internet Overwatch</h1>

<p align="center"><em>Monitor. Analyze. Protect. Play.</em></p>

A desktop network monitor that answers one question properly:

> **Why am I lagging?**

A single ping number cannot tell you whether the problem is your Wi-Fi, your
ISP, or the game server. Internet Overwatch measures **three layers at once** —
your router, several independent public targets, and your game server — and
compares them. When latency spikes, the comparison is what makes an answer
possible:

```
Router      2 ms  HEALTHY     Router    220 ms  PROBLEM
Internet   21 ms  HEALTHY     Internet  230 ms  PROBLEM
Game      490 ms  PROBLEM     Game      290 ms  PROBLEM
-> destination-specific       -> local network problem
```

---

## What it does

- **Continuous multi-layer monitoring** — ICMP, TCP-connect and DNS probes on
  independent per-target intervals, none of it on the UI thread.
- **Adaptive lag detection** — spikes are measured against a rolling median
  baseline, not a fixed threshold, so it works whether your normal ping is
  12 ms or 85 ms.
- **Incidents, not noise** — a three-second lag event is recorded as *one*
  incident with a start, end, peak, duration and severity, rather than forty
  separate rows.
- **Automatic diagnosis with confidence** — every conclusion is graded
  *Likely* / *Possible* / *No clear cause*, and backed by the evidence used to
  reach it. It never claims certainty it does not have.
- **Full history** — SQLite storage with sessions, percentiles (p95/p99),
  historical graphs and configurable retention.
- **Reports** — a plain-text diagnostic report, plus an evidence-oriented ISP
  report designed to attach to a support ticket.
- **Diagnostics** — traceroute, DNS timing, adapter and Wi-Fi details, and a
  manual bufferbloat test.
- **Gaming mode and overlay** — a small always-on-top metrics window, with
  reduced UI updates so monitoring costs you nothing while you play.
- **Local-first** — no telemetry, no account, no data leaves your machine.
  Traffic goes only to the targets you configure.

---

## Install and run

Requires **Python 3.11+** (3.12 recommended).

```bash
pip install -r requirements.txt
python -m app.main
```

On first launch a short setup detects your adapter, gateway and DNS servers,
then asks what to monitor.

### Command line

```bash
python -m app.main                          # normal GUI
python -m app.main --simulate isp_spikes    # synthetic data, no real network needed
python -m app.main --headless --duration 60 # monitor for 60s and print a report
python -m app.main --debug                  # verbose logging
python -m app.main --reset-settings         # restore defaults
```

`--headless` is the quickest way to see the engine work — it prints a full
diagnostic report to the terminal.

---

## How the diagnosis works

Every measurement flows through one pipeline:

```
Probe -> Measurement -> Validation -> Ring buffer (live graph)
                                   -> SQLite (batched writes)
                                   -> Detector -> Incident
                                              -> Correlation -> Diagnosis -> UI
```

When a spike is detected, the engine looks at every other target inside a
±2 second window around it and applies three rules:

| Rule | Condition | Conclusion |
|------|-----------|------------|
| **A** | Gateway itself is slow, lossy or spiking | Local network issue |
| **B** | Gateway healthy, several public targets degraded together | ISP / internet path issue |
| **C** | Gateway and public targets healthy, only the destination degraded | Destination-specific instability |

The key question — *did the router spike at the same moment?* — is answered by
data rather than guessed, and the answer appears in the report:

```
Local gateway remained stable during 6 of 6 incidents.
Public internet targets remained stable during 0 of 6 incidents.
```

---

## Project layout

```
app/
├── main.py               entry point, CLI, first-run
├── config/               defaults and persisted settings (every threshold in one place)
├── core/                 monitor, scheduler, detector, diagnostics, health score, simulator
├── network/              ping, gateway, traceroute, dns, throughput, wifi, interfaces
├── storage/              SQLite schema, models, repository
├── services/             Qt monitoring service, reports, exports
├── ui/                   theme, main window, widgets/, pages/
└── utils/                logging, time formatting, platform, assets, autostart
tests/                    166 tests, no network required
```

The monitoring engine is **Qt-free** — it communicates through plain callbacks
that the Qt service layer adapts into signals. That is what keeps the UI
responsive and the whole engine unit-testable.

---

## Testing

```bash
python -m pytest
```

The suite covers baseline and jitter maths, packet loss windows, spike
detection and severity, incident merging, all three diagnostic rules,
correlation, health scoring, the storage layer and the full event pipeline.

A built-in simulator generates nine failure scenarios so the dashboard and the
detection rules can be exercised without waiting for a real fault:

```bash
python -m app.main --simulate destination_spikes
```

`stable`, `high_latency`, `jitter`, `packet_loss`, `router_spikes`,
`isp_spikes`, `destination_spikes`, `complete_outage`, `bufferbloat_pattern`

---

## Building a Windows executable

```bash
pip install pyinstaller
python -m app.utils.assets          # regenerate assets/icons/app_icon.ico from the logo
pyinstaller internet_overwatch.spec
```

The result is `dist/InternetOverwatch.exe`. "Start when I sign in" is available
in Settings and registers a per-user entry that is trivial to remove.

---

## Notes on measurement honesty

A few things the app is deliberately careful about, because getting them wrong
would produce confident nonsense:

- **TCP connect time is not in-game latency.** Targets using it are labelled as
  such throughout the UI.
- **Traceroute hop latency is not proof of a broken hop.** Routers routinely
  deprioritise ICMP. The UI repeats this caveat next to every trace.
- **Weak Wi-Fi signal is not proof of a latency problem.** It is reported as
  information, with hedged wording.
- **The health score is an indicator, not a measurement.** It says so on the
  dashboard.
- **The gateway address is never hardcoded.** It is read from the routing
  table; if detection fails, the app says so rather than assuming
  `192.168.1.1`.

---

## Configuration

Settings and the database live next to the application in `data/`, or in your
user application-data directory if that location is not writable. Everything is
editable from the Settings page: intervals, timeouts, spike sensitivity,
severity bands, retention, notifications and logging.

## License

MIT — see [LICENSE](LICENSE).
