# Internet Overwatch — Full Project Plan

## 1. Vision

**Internet Overwatch** is a desktop network-monitoring and diagnostics application built in Python.

The goal is not merely to show a ping number. The application should continuously answer:

1. Is my local network healthy?
2. Is my ISP/internet path healthy?
3. Is the route to my game/server healthy?
4. When did the problem start?
5. How severe was it?
6. Which hop or layer is most likely responsible?
7. Can I prove the problem with historical data?

The application should run quietly while gaming, collect measurements with minimal overhead, visualize them in a modern dashboard, detect lag spikes automatically, and produce useful diagnostic reports.

---

# 2. Primary Use Case

The user starts Internet Overwatch before playing.

The application monitors:

```text
PC
 │
 ├── Local Gateway / Router
 │
 ├── ISP / First Internet Hop
 │
 ├── Public Internet Targets
 │      ├── Cloudflare
 │      ├── Google
 │      └── Custom targets
 │
 └── Game / Custom Server
```

During a lag event the application records:

```text
Timestamp
Latency
Jitter
Packet loss
Target
Local gateway latency
Internet latency
Network throughput
System network activity
Current route information
Spike severity
```

The user can later inspect exactly what happened.

---

# 3. Core Design Principle

## Never rely on a single ping target

A game showing 300 ms does not automatically mean the user's internet connection is bad.

We therefore measure multiple layers.

### Layer 1 — Local network

Example:

```text
PC → Router
```

This detects:

- Wi-Fi problems
- LAN congestion
- router overload
- wireless interference
- local packet loss

### Layer 2 — Internet / ISP

Example:

```text
PC → 1.1.1.1
```

This detects:

- ISP instability
- upstream latency
- packet loss
- routing problems

### Layer 3 — Game/custom destination

Example:

```text
PC → Game server
```

This detects:

- game-server route problems
- destination-specific problems
- regional routing problems

### Diagnostic comparison

```text
Router      2 ms   🟢
Internet   20 ms   🟢
Game       85 ms   🟡
```

versus:

```text
Router      2 ms   🟢
Internet   22 ms   🟢
Game      500 ms   🔴
```

versus:

```text
Router    220 ms   🔴
Internet  230 ms   🔴
Game      290 ms   🔴
```

The dashboard should use these relationships to generate a probable diagnosis.

---

# 4. Technology Stack

## Language

```text
Python 3.12+
```

Recommended minimum:

```text
Python 3.11+
```

## GUI

Recommended:

```text
PySide6
```

Reason:

- modern desktop UI
- excellent charts/widgets
- Windows support
- scalable architecture
- Qt threading/signals
- easy dark theme
- suitable for a professional dashboard

## Charts

Primary:

```text
PyQtGraph
```

Alternative:

```text
QtCharts
```

PyQtGraph is preferred for high-frequency live data.

## Storage

Development:

```text
SQLite
```

Optional:

```text
SQLModel / SQLAlchemy
```

SQLite is sufficient for local monitoring.

## Networking

Python standard library where possible:

```text
socket
subprocess
asyncio
statistics
ipaddress
```

Potential libraries:

```text
ping3
psutil
scapy
dnspython
```

Use platform-specific native tools when they provide more reliable results.

## Packaging

```text
PyInstaller
```

Target:

```text
Windows 10/11
```

Future:

```text
Linux
macOS
```

---

# 5. Project Structure

```text
internet-overwatch/
│
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
│
├── app/
│   ├── __init__.py
│   │
│   ├── main.py
│   │
│   ├── config/
│   │   ├── settings.py
│   │   └── defaults.py
│   │
│   ├── core/
│   │   ├── monitor.py
│   │   ├── scheduler.py
│   │   ├── detector.py
│   │   ├── diagnostics.py
│   │   └── health_score.py
│   │
│   ├── network/
│   │   ├── ping.py
│   │   ├── gateway.py
│   │   ├── traceroute.py
│   │   ├── dns.py
│   │   ├── throughput.py
│   │   ├── wifi.py
│   │   └── interfaces.py
│   │
│   ├── storage/
│   │   ├── database.py
│   │   ├── models.py
│   │   └── repository.py
│   │
│   ├── ui/
│   │   ├── main_window.py
│   │   ├── theme.py
│   │   ├── widgets/
│   │   │   ├── latency_card.py
│   │   │   ├── status_card.py
│   │   │   ├── live_graph.py
│   │   │   ├── packet_loss.py
│   │   │   ├── network_map.py
│   │   │   └── event_log.py
│   │   │
│   │   └── pages/
│   │       ├── overview.py
│   │       ├── live_monitor.py
│   │       ├── diagnostics.py
│   │       ├── history.py
│   │       ├── targets.py
│   │       └── settings.py
│   │
│   ├── services/
│   │   ├── monitoring_service.py
│   │   ├── report_service.py
│   │   └── export_service.py
│   │
│   └── utils/
│       ├── logger.py
│       ├── time.py
│       └── platform.py
│
├── tests/
│   ├── test_ping.py
│   ├── test_detector.py
│   ├── test_diagnostics.py
│   ├── test_health_score.py
│   └── test_storage.py
│
├── data/
│   └── .gitkeep
│
└── assets/
    ├── icons/
    └── fonts/
```

---

# 6. Dashboard

The dashboard is the main feature.

The application should open directly into:

```text
OVERVIEW
```

Recommended layout:

```text
┌────────────────────────────────────────────────────────────────────┐
│ INTERNET OVERWATCH                         ● MONITORING     ⚙      │
├──────────────┬─────────────────────────────────────────────────────┤
│              │                                                     │
│ OVERVIEW     │  INTERNET HEALTH                                  │
│              │                                                     │
│ LIVE         │  82 / 100                         ● STABLE         │
│              │                                                     │
│ DIAGNOSTICS  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐     │
│              │  │  81ms  │ │  12ms  │ │  0.2%  │ │   4    │     │
│ HISTORY      │  │ LATENCY│ │ JITTER │ │  LOSS  │ │ SPIKES │     │
│              │  └────────┘ └────────┘ └────────┘ └────────┘     │
│ TARGETS      │                                                     │
│              │  LATENCY TIMELINE                                  │
│ SETTINGS     │  500 ┤                           ╭╮                 │
│              │  300 ┤                  ╭╮       ││                 │
│              │  150 ┤     ╭╮           ││  ╭╮  ││                 │
│              │   80 ┼─────┴┴───────────┴┴──┴┴──┴────             │
│              │       18:00       18:15       18:30                │
│              │                                                     │
│              │  CONNECTION PATH                                   │
│              │  PC ── 2ms ── Router ── 19ms ── ISP ── 81ms ── Game│
│              │                                                     │
│              │  RECENT EVENTS                                     │
│              │  🔴 18:27:13  Severe lag spike: 487ms             │
│              │  🟡 18:24:51  Jitter increased to 42ms            │
│              │  🟢 18:21:08  Connection stabilized               │
│              │                                                     │
└──────────────┴─────────────────────────────────────────────────────┘
```

---

# 7. Sidebar

Navigation:

```text
Overview
Live Monitor
Diagnostics
History
Targets
Settings
```

Optional:

```text
About
```

Sidebar should remain fixed.

---

# 8. Overview Page

## 8.1 Health Score

Large central score:

```text
82 / 100
```

Status:

```text
STABLE
```

Possible statuses:

```text
EXCELLENT
GOOD
STABLE
UNSTABLE
POOR
CRITICAL
```

Health score is calculated from:

```text
Latency
Jitter
Packet loss
Spike frequency
Gateway health
Internet target health
```

Do not pretend the score is scientifically exact.

It is a user-friendly diagnostic indicator.

---

# 9. Metric Cards

Display:

### Latency

```text
81 ms
```

Include:

```text
Current
Average
Min
Max
```

### Jitter

```text
12 ms
```

### Packet Loss

```text
0.2 %
```

### Lag Spikes

```text
4
```

Show count for selected time period.

### Uptime

```text
2h 14m
```

### Network state

```text
● Monitoring
```

---

# 10. Live Latency Graph

The most important UI component.

Graph should show:

```text
Router
Internet
Game
```

as separate series.

Example:

```text
Latency
600 ┤
500 ┤                         ●
400 ┤                         │
300 ┤                  ●      │
200 ┤                  │      │
100 ┤───────╮──────────╯──────╯────
 50 ┤       ╰──────────────────────
  0 ┼────────────────────────────────
       18:00   18:10   18:20   18:30
```

Features:

- zoom
- pan
- pause
- reset
- hover values
- spike markers
- time range
- target toggle
- average line
- threshold line

Time ranges:

```text
1 min
5 min
15 min
1 hour
6 hours
24 hours
7 days
```

---

# 11. Spike Visualization

Every detected spike should be marked.

Example:

```text
             🔴
             │
─────────────●────────────
             487ms
             18:27:13
```

Clicking the spike opens:

```text
Lag Spike

Time:
18:27:13.442

Normal:
81 ms

Peak:
487 ms

Duration:
1.2 seconds

Router:
3 ms

Internet:
22 ms

Game:
487 ms

Packet loss:
0%

Likely cause:
Destination-specific latency increase
```

---

# 12. Connection Path Visualization

Display the network path as a visual chain.

```text
┌──────┐
│  PC  │
└──┬───┘
   │ 2 ms
   ▼
┌────────┐
│ Router │
└───┬────┘
    │ 19 ms
    ▼
┌────────┐
│  ISP   │
└───┬────┘
    │ 23 ms
    ▼
┌──────────┐
│ Internet │
└────┬─────┘
     │ 81 ms
     ▼
┌─────────────┐
│ Game Server │
└─────────────┘
```

Each node displays:

```text
Latency
Packet loss
Status
```

Statuses:

```text
🟢 Healthy
🟡 Warning
🔴 Problem
⚪ Unknown
```

---

# 13. Recent Events

Event feed:

```text
18:27:13 🔴 Severe latency spike
18:24:51 🟡 High jitter
18:21:08 🟢 Connection stabilized
18:19:32 🟡 Packet loss detected
18:10:01 🟢 Monitoring started
```

Clicking an event should open detailed information.

---

# 14. Live Monitor Page

This page is optimized for gaming.

Minimal UI:

```text
┌──────────────────────────────────────────────────────────┐
│ LIVE MONITOR                                             │
│                                                          │
│              81 ms                                       │
│              LATENCY                                     │
│                                                          │
│       JITTER       LOSS        SPIKES                    │
│        12 ms       0.2%          4                       │
│                                                          │
│  ● Router       2 ms                                  │
│  ● Internet    21 ms                                  │
│  ● Game        81 ms                                  │
│                                                          │
│  STATUS: 🟡 UNSTABLE                                     │
└──────────────────────────────────────────────────────────┘
```

Optional compact mode:

```text
OW  ● 81ms  J:12ms  L:0.2%  ⚠
```

This can eventually be displayed as a small always-on-top overlay.

---

# 15. Diagnostics Page

This page answers:

> Why am I lagging?

Sections:

## Local Network

```text
Gateway reachable: YES
Gateway latency: 2 ms
Gateway loss: 0%
```

Result:

```text
🟢 Local network appears healthy
```

## Internet

```text
1.1.1.1: 21 ms
8.8.8.8: 24 ms
Packet loss: 0%
```

Result:

```text
🟢 Internet connection appears healthy
```

## Destination

```text
Game target: 81 ms
Jitter: 37 ms
Spikes: frequent
```

Result:

```text
🟡 Destination path appears unstable
```

---

# 16. Automatic Diagnosis Engine

The application should never claim certainty unless it actually has evidence.

Use wording such as:

```text
Likely cause
Possible cause
Evidence suggests
No clear cause detected
```

Never:

```text
Your ISP is definitely broken.
```

unless the data genuinely establishes that.

---

# 17. Diagnostic Rules

## Rule A — Local network problem

If:

```text
gateway latency > threshold
```

or:

```text
gateway packet loss > 0
```

then:

```text
Possible local network issue.
```

Likely causes:

- Wi-Fi interference
- weak signal
- router load
- LAN congestion
- network adapter issues

---

# 18. Rule B — Internet problem

If:

```text
gateway healthy
AND
multiple public targets degraded
```

then:

```text
Possible ISP/internet path issue.
```

---

# 19. Rule C — Destination-specific issue

If:

```text
gateway healthy
AND
public targets healthy
AND
game target degraded
```

then:

```text
Issue appears destination-specific.
```

---

# 20. Rule D — Packet Loss

If packet loss occurs:

```text
0%
```

healthy.

```text
0–1%
```

minor.

```text
1–3%
```

warning.

```text
>3%
```

serious.

These values should be configurable.

---

# 21. Rule E — Jitter

Example default thresholds:

```text
< 10 ms       Excellent
10–25 ms      Good
25–50 ms      Warning
> 50 ms       Poor
```

Again, configurable.

---

# 22. Rule F — Lag Spike

A spike should not simply mean:

```text
latency > 100 ms
```

because the user's normal latency might already be 80 ms.

Instead calculate a baseline.

Example:

```text
Baseline = rolling median of recent samples
```

Then detect:

```text
current_latency > baseline + absolute_threshold
```

and/or:

```text
current_latency > baseline * multiplier
```

Example:

```text
Baseline: 81 ms
Current: 280 ms

Difference: +199 ms
Ratio: 3.46x
```

This is a clear spike.

---

# 23. Spike Severity

## Minor

```text
+50–100 ms
```

## Moderate

```text
+100–250 ms
```

## Severe

```text
+250–500 ms
```

## Critical

```text
>500 ms
```

Thresholds configurable.

---

# 24. Packet Loss Detection

Store each probe:

```text
timestamp
target
success
latency
error
```

Example:

```text
18:30:01 success 21ms
18:30:02 success 22ms
18:30:03 timeout
18:30:04 success 23ms
```

Calculate:

```text
packet_loss = failed_probes / total_probes
```

Use sliding windows.

---

# 25. Jitter Calculation

Preferred simple method:

```text
abs(latency[i] - latency[i-1])
```

Calculate rolling average/median.

More advanced implementation can later include:

```text
RFC-style variation metrics
```

but MVP should remain simple and understandable.

---

# 26. Monitoring Frequency

Default:

```text
Router: 250 ms
Internet targets: 500 ms
Game target: 250–500 ms
```

Allow user configuration.

Do not flood targets.

Recommended safeguards:

```text
Minimum interval: 100 ms
```

and per-target rate limiting.

---

# 27. Targets

The Targets page allows:

```text
Add target
Edit target
Remove target
Enable/disable target
```

Target types:

```text
IP
Hostname
URL/DNS
Game server
```

Each target:

```text
Name
Host
Port
Protocol
Interval
Enabled
Category
```

Example:

```text
Router
192.168.1.1

Cloudflare
1.1.1.1

Google DNS
8.8.8.8

Game Server
custom-host.example
```

---

# 28. Game Server Support

Do not assume every game exposes a simple ICMP ping endpoint.

Support:

```text
ICMP
TCP connect latency
DNS
UDP where appropriate
```

Game-specific integrations can be added later.

Important:

A TCP connection measurement is not equivalent to actual in-game network latency.

The UI should clearly label the measurement type.

---

# 29. Traceroute

Diagnostics should support:

```text
Windows: tracert
Linux/macOS: traceroute
```

Optional advanced implementation:

```text
scapy
```

Display:

```text
Hop 1    192.168.1.1       2 ms
Hop 2    ISP gateway      14 ms
Hop 3    ISP backbone     18 ms
Hop 4    ...              21 ms
Hop 5    ...              83 ms
```

Important:

Traceroute latency alone must not be interpreted as proof of a broken hop because routers may deprioritize ICMP.

---

# 30. DNS Monitoring

Measure DNS response time.

Example:

```text
Resolver
1.1.1.1

DNS latency:
18 ms

Failures:
0
```

Allow custom DNS targets.

---

# 31. Throughput Monitoring

Passive network usage:

```text
Download:
12.4 Mbps

Upload:
1.2 Mbps
```

Use `psutil` where possible.

Do not continuously run speed tests.

Speed tests should be manual or scheduled.

---

# 32. Bufferbloat Test

Add a manual diagnostic.

Concept:

1. Measure baseline latency.
2. Saturate download.
3. Measure latency while saturated.
4. Saturate upload.
5. Measure latency while saturated.
6. Compare latency increase.

Example:

```text
Idle latency:
22 ms

Download latency:
184 ms

Increase:
+162 ms

Result:
Severe bufferbloat
```

Do not run automatically during gaming.

---

# 33. Wi-Fi Information

Windows support should collect:

```text
SSID
BSSID
Signal %
Channel
Radio type
Receive rate
Transmit rate
```

Where OS APIs allow it.

The UI:

```text
Wi-Fi
SSID: MyNetwork
Signal: 72%
Channel: 36
Link speed: 866 Mbps
```

Potential warnings:

```text
Weak Wi-Fi signal
Possible wireless instability
```

Do not treat signal strength alone as proof of latency problems.

---

# 34. Network Adapter Information

Display:

```text
Interface
IPv4
IPv6
Gateway
DNS
Link speed
Connection type
```

Example:

```text
Ethernet
192.168.1.50
Gateway 192.168.1.1
1 Gbps
```

---

# 35. History Page

The user can inspect previous sessions.

Example:

```text
Today
────────────────────────────
18:00–19:00
Health: 84
Spikes: 13
Loss: 0.2%

19:00–20:00
Health: 61
Spikes: 47
Loss: 1.4%
```

Click session:

```text
Session details
```

---

# 36. Historical Graph

Support:

```text
1 hour
6 hours
12 hours
24 hours
7 days
30 days
```

Graph:

```text
Average latency
95th percentile
Packet loss
Spike count
Health score
```

---

# 37. Percentiles

Store enough information to calculate:

```text
min
average
median
p95
p99
max
```

Example:

```text
Average: 81 ms
Median: 78 ms
P95: 124 ms
P99: 384 ms
Max: 712 ms
```

Percentiles are especially useful because a low average can hide severe spikes.

---

# 38. Session System

A monitoring session starts when:

```text
application starts monitoring
```

and ends when:

```text
monitoring stops
```

Store:

```text
session_id
start_time
end_time
targets
statistics
events
```

---

# 39. Database Schema

## sessions

```text
id
start_time
end_time
name
```

## targets

```text
id
name
host
port
protocol
interval_ms
enabled
```

## samples

```text
id
session_id
target_id
timestamp
latency_ms
success
error_type
```

## events

```text
id
session_id
timestamp
type
severity
target_id
message
metadata_json
```

## system_samples

```text
id
session_id
timestamp
download_bps
upload_bps
cpu_percent
memory_percent
```

---

# 40. Data Retention

Default:

```text
30 days
```

Configurable:

```text
7 days
30 days
90 days
1 year
Forever
```

Provide:

```text
Delete history
Clear database
Export database
```

---

# 41. CSV Export

Allow:

```text
Export session → CSV
```

Example:

```text
timestamp,target,latency_ms,success
18:30:01,router,2.1,true
18:30:02,router,2.4,true
18:30:03,router,,false
```

---

# 42. Diagnostic Report

Generate a human-readable report.

Example:

```text
INTERNET OVERWATCH REPORT

Session:
2026-08-30 18:00–20:00

Overall Health:
72 / 100

Latency:
Average: 81ms
Median: 78ms
P95: 140ms
Max: 712ms

Packet Loss:
0.8%

Lag Spikes:
37

Router:
Healthy

Internet:
Healthy

Game Target:
Unstable

Likely finding:
Destination-specific latency instability.

Evidence:
Router remained below 5ms during 31 of 37 detected spikes.
Public targets remained stable during most events.
Game target experienced the largest latency increases.
```

This report is intended to help when contacting an ISP or game support.

---

# 43. Notifications

Optional desktop notifications.

Example:

```text
⚠ Internet Overwatch

Severe lag spike detected:
81ms → 492ms
```

Settings:

```text
Enable notifications
Minimum severity
Cooldown
```

Avoid notification spam.

---

# 44. Sound

Optional.

Default:

```text
OFF
```

Could provide:

```text
Minor
Warning
Critical
```

But visual notifications should be the primary mechanism.

---

# 45. Overlay Mode

Future feature.

Small transparent always-on-top window:

```text
┌─────────────────────────────┐
│ ● 81ms   J 12ms   L 0.2%   │
└─────────────────────────────┘
```

Optional:

```text
FPS
```

if game integration is added later.

Overlay should have:

```text
opacity
position
font size
visible metrics
hotkey
```

---

# 46. Gaming Mode

A dedicated profile:

```text
GAMING MODE
```

When enabled:

- reduce UI updates
- keep monitoring active
- disable heavy diagnostics
- disable automatic speed tests
- prioritize game target
- optionally enable overlay
- increase event detail

---

# 47. Monitoring Architecture

Use separate workers.

```text
                 ┌──────────────────┐
                 │ Monitoring Core  │
                 └────────┬─────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
    Ping Worker      System Worker    Diagnostics
         │                │                │
         └────────────────┼────────────────┘
                          ▼
                   Event Processor
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
          Database                 UI Signals
```

The GUI must never block on network operations.

---

# 48. Threading

Recommended:

```text
QThread
```

or:

```text
QThreadPool
QRunnable
```

For asynchronous monitoring:

```text
asyncio
```

can be used internally, but Qt integration should remain clean.

Do not run ping calls directly in the main UI thread.

---

# 49. Event Pipeline

Every measurement follows:

```text
Probe
 ↓
Measurement
 ↓
Validation
 ↓
Storage
 ↓
Detector
 ↓
Event
 ↓
Diagnostic Engine
 ↓
UI
```

Example:

```text
Ping = 487ms
 ↓
Baseline = 81ms
 ↓
Spike detected
 ↓
Severity = Severe
 ↓
Check router
 ↓
Router = 2ms
 ↓
Check public targets
 ↓
Healthy
 ↓
Diagnosis:
Destination-specific instability
```

---

# 50. In-Memory Ring Buffer

Do not query SQLite for every graph frame.

Keep recent data in memory:

```text
deque(maxlen=...)
```

Example:

```text
last 5 minutes
```

The graph reads from the ring buffer.

SQLite stores the persistent history.

---

# 51. UI Update Frequency

Network sampling can be frequent.

UI should not necessarily update at the same rate.

Recommended:

```text
sampling: 250–500 ms
UI: 100–250 ms
database batch writes: 1–5 seconds
```

This prevents unnecessary CPU usage.

---

# 52. Performance Requirements

Target:

```text
CPU:
<2–5% idle monitoring

RAM:
<150 MB target

Network overhead:
minimal
```

Actual values should be measured and optimized later.

---

# 53. Dark Theme

Primary design:

```text
Dark
```

Style:

- dark background
- subtle borders
- rounded cards
- high readability
- restrained accent colors
- clear warning states

Avoid making the entire application neon.

Use color primarily for state:

```text
Green = healthy
Yellow = warning
Red = problem
Gray = unknown
```

---

# 54. Dashboard Design Language

Visual hierarchy:

```text
1. Current health
2. Current latency
3. Live graph
4. Connection path
5. Events
6. Details
```

The user should understand the state within 2–3 seconds.

---

# 55. Responsive Layout

Minimum window:

```text
1200 × 700
```

Recommended:

```text
1440 × 900
```

The layout should scale.

At smaller resolutions:

- collapse sidebar
- reduce graph height
- stack metric cards

---

# 56. Settings

Settings categories:

## Monitoring

```text
Router interval
Internet interval
Game interval
Timeout
Spike sensitivity
```

## Targets

```text
Default targets
```

## Appearance

```text
Theme
Scale
Compact mode
```

## Notifications

```text
Enabled
Cooldown
Severity
```

## Storage

```text
Retention
Database location
```

## Advanced

```text
Traceroute command
Ping implementation
Debug logging
```

---

# 57. First-Run Setup

On first launch:

```text
Welcome to Internet Overwatch

Let's configure your connection.
```

Automatically detect:

```text
Default network adapter
Default gateway
DNS servers
Public IP (optional)
```

Then ask:

```text
What do you want to monitor?

☑ Local network
☑ Internet
☑ Custom server
```

Finish:

```text
Monitoring is ready.
```

---

# 58. Automatic Gateway Detection

Use routing table / OS networking information to detect the default gateway.

Windows:

```text
ipconfig
route
```

or suitable native APIs.

Do not hardcode:

```text
192.168.1.1
```

because the user's router may use another subnet.

---

# 59. Error Handling

The application must handle:

```text
No internet
No gateway
DNS failure
Target timeout
Permission errors
IPv4 unavailable
IPv6 unavailable
Network adapter disconnected
```

Never crash the entire UI because one target fails.

---

# 60. Offline State

If all external targets fail but router responds:

```text
LOCAL NETWORK OK
INTERNET UNREACHABLE
```

If router itself fails:

```text
LOCAL NETWORK UNREACHABLE
```

If only one target fails:

```text
TARGET UNREACHABLE
```

---

# 61. IPv4 / IPv6

Support both where possible.

Display:

```text
IPv4
IPv6
```

Allow:

```text
Prefer IPv4
Prefer IPv6
Auto
```

This can be useful for diagnosing routing differences.

---

# 62. Security / Privacy

The application should be local-first.

Default:

```text
No telemetry
No cloud account
No external data upload
```

Only send traffic to targets explicitly configured by the user.

Do not collect:

```text
passwords
browser history
private packet contents
```

---

# 63. Logging

Application logs:

```text
logs/app.log
```

Levels:

```text
DEBUG
INFO
WARNING
ERROR
```

Normal users should not need to inspect logs.

Provide:

```text
Open Logs
```

in Advanced Settings.

---

# 64. Testing Strategy

## Unit Tests

Test:

```text
baseline calculation
jitter calculation
packet loss
spike detection
severity
health score
diagnostic rules
database writes
```

## Integration Tests

Test:

```text
monitor → database
monitor → detector
detector → UI
```

## Failure Tests

Simulate:

```text
timeouts
packet loss
500ms latency
router failure
target failure
```

---

# 65. Synthetic Test Data

Build a simulator so the dashboard can be tested without real network problems.

Example scenarios:

```text
stable
high latency
jitter
packet loss
local router spikes
ISP spikes
destination spikes
complete outage
```

This is extremely useful for UI development.

---

# 66. Example Synthetic Scenario

```text
Baseline:
25ms

Samples:
24
26
25
27
29
181
430
27
26
25
```

Expected:

```text
2 spikes

Severity:
Severe

Router:
healthy

Internet:
degraded
```

---

# 67. Health Score

Suggested initial model:

```text
100 points
```

Subtract for:

```text
latency degradation
jitter
packet loss
spike frequency
outages
```

Example:

```text
Latency score      90
Jitter score       70
Loss score         100
Spike score        50
Local network      100
```

Weighted result:

```text
82
```

Weights should be configurable in code, not hardcoded throughout the application.

---

# 68. Important Diagnostic Limitation

The application cannot always determine the exact cause of a network problem.

For example:

```text
Game server = 400ms
```

does not prove:

```text
ISP problem
```

The diagnostic engine should therefore use confidence:

```text
LIKELY
POSSIBLE
UNCLEAR
```

Example:

```text
Likely destination-specific issue
Confidence: Medium
```

---

# 69. ISP Evidence Mode

Add a special button:

```text
CREATE ISP REPORT
```

The report should contain:

```text
Test period
Average latency
P95
P99
Packet loss
Spike count
Router comparison
Multiple external targets
Traceroute
Timeline
```

The report should be easy to attach to support tickets.

---

# 70. Event Correlation

This is one of the most important advanced features.

Suppose:

```text
18:32:01
Router = 2ms
Cloudflare = 21ms
Game = 80ms

18:32:02
Router = 2ms
Cloudflare = 22ms
Game = 490ms
```

The engine should correlate the event.

Result:

```text
Game latency spike detected.

Local gateway remained stable.
Public internet targets remained stable.

Likely destination-specific issue.
```

---

# 71. Correlation Window

Default:

```text
±2 seconds
```

around a spike.

Compare all monitored targets inside that window.

This creates the foundation for meaningful diagnosis.

---

# 72. Multi-Target Health

Example:

```text
Router        🟢
Cloudflare    🟢
Google        🟢
ISP target    🟡
Game          🔴
```

Overall result:

```text
Internet mostly healthy
Destination unstable
```

---

# 73. Advanced Future Feature — Route Comparison

Save traceroutes over time.

Compare:

```text
Monday
Tuesday
Today
```

Detect:

```text
route changed
```

Example:

```text
Previous:
PC → ISP A → Backbone B → Game

Current:
PC → ISP A → Backbone C → Game
```

This could explain why the problem only happens at certain times.

---

# 74. Advanced Future Feature — ISP Monitoring

Allow scheduled long-term tests:

```text
Every 5 minutes
```

Store:

```text
latency
loss
jitter
```

Then produce:

```text
24h report
7d report
30d report
```

Useful for intermittent problems.

---

# 75. Advanced Future Feature — Network Map

Visual graph:

```text
PC
 │
 ▼
Router
 │
 ▼
ISP
 │
 ├── Target A
 ├── Target B
 └── Game
```

Click a node:

```text
Latency
Loss
Historical health
```

---

# 76. Advanced Future Feature — Game Profiles

Allow:

```text
Profile: Valorant
Profile: CS2
Profile: Fortnite
Profile: Custom
```

Each profile stores:

```text
targets
thresholds
overlay settings
sampling interval
```

Do not rely on game-specific server addresses unless officially documented or manually supplied.

---

# 77. Advanced Future Feature — Discord/Webhook

Optional integrations:

```text
Discord webhook
```

Example:

```text
🔴 Internet Overwatch

Severe lag spike detected.
Peak: 612ms
Baseline: 23ms
Packet loss: 1.8%
```

This should be opt-in.

---

# 78. Advanced Future Feature — Local Web Dashboard

Optional future architecture:

```text
Python backend
       │
       ▼
Local API
       │
       ▼
Web dashboard
```

Possible:

```text
FastAPI
React
```

But this should NOT be the MVP.

PySide6 is the recommended first dashboard.

---

# 79. MVP Definition

Version 0.1 is complete when it can:

- detect gateway
- ping gateway
- ping at least two public targets
- monitor one custom target
- calculate latency
- calculate jitter
- calculate packet loss
- detect spikes
- store samples
- display live graph
- show current status
- show recent events
- run without freezing the UI

---

# 80. Version 0.2

Add:

- SQLite history
- History page
- diagnostics engine
- health score
- CSV export
- target management
- settings

---

# 81. Version 0.3

Add:

- traceroute
- DNS diagnostics
- Wi-Fi information
- ISP report
- advanced graph
- percentile statistics

---

# 82. Version 0.4

Add:

- gaming mode
- overlay
- notifications
- bufferbloat test
- synthetic test mode

---

# 83. Version 1.0

Version 1.0 should feel like a complete product:

```text
Professional dashboard
Reliable monitoring
Historical data
Diagnostics
Reports
Gaming mode
Overlay
Configuration
Packaging
```

---

# 84. Recommended Development Order

## Phase 1 — Network engine

Build:

```text
gateway detection
ping
targets
measurements
```

No fancy UI yet.

---

## Phase 2 — Detection

Build:

```text
rolling baseline
jitter
packet loss
spikes
severity
```

---

## Phase 3 — Storage

Build:

```text
SQLite
sessions
samples
events
```

---

## Phase 4 — Dashboard

Build:

```text
main window
sidebar
metric cards
live graph
event list
```

---

## Phase 5 — Diagnostics

Build:

```text
correlation
health score
diagnosis
confidence
```

---

## Phase 6 — History

Build:

```text
session browser
historical graphs
statistics
export
```

---

## Phase 7 — Advanced Diagnostics

Build:

```text
traceroute
DNS
Wi-Fi
bufferbloat
```

---

## Phase 8 — Gaming UX

Build:

```text
gaming mode
overlay
notifications
```

---

## Phase 9 — Packaging

Build:

```text
Windows executable
installer
auto-start option
```

---

# 85. MVP UI Component Tree

```text
MainWindow
│
├── Sidebar
│   ├── OverviewButton
│   ├── LiveButton
│   ├── DiagnosticsButton
│   ├── HistoryButton
│   ├── TargetsButton
│   └── SettingsButton
│
└── ContentStack
    │
    ├── OverviewPage
    │   ├── HealthCard
    │   ├── MetricCards
    │   ├── LatencyGraph
    │   ├── ConnectionPath
    │   └── EventList
    │
    ├── LiveMonitorPage
    │   ├── BigLatency
    │   ├── MiniMetrics
    │   └── TargetList
    │
    ├── DiagnosticsPage
    │   ├── LocalNetworkCard
    │   ├── InternetCard
    │   ├── DestinationCard
    │   └── DiagnosisCard
    │
    ├── HistoryPage
    │   ├── SessionList
    │   └── HistoricalGraph
    │
    ├── TargetsPage
    │   └── TargetTable
    │
    └── SettingsPage
```

---

# 86. MVP Data Flow

```text
Ping Worker
     │
     ▼
Measurement
     │
     ├───────────────┐
     ▼               ▼
Ring Buffer       Detector
     │               │
     ▼               ▼
Live Graph        Event
                     │
                     ▼
              Diagnostic Engine
                     │
                     ▼
                Health Score
                     │
                     ▼
                    UI
                     │
                     ▼
                  SQLite
```

---

# 87. Definition of a Good Lag Detector

A good detector should:

- adapt to the user's normal latency
- ignore tiny fluctuations
- detect sudden spikes
- detect sustained degradation
- avoid duplicate events
- record duration
- record peak
- compare multiple targets

Bad detector:

```text
if ping > 100:
    lag = True
```

Good detector:

```text
baseline = rolling_median(...)
deviation = current - baseline

if deviation > threshold and current > baseline * multiplier:
    create_spike_event()
```

---

# 88. Event Deduplication

Do not generate:

```text
487ms spike
492ms spike
503ms spike
510ms spike
```

as four separate events if they are one continuous incident.

Instead:

```text
Lag incident

Start: 18:27:13
End: 18:27:16
Peak: 510ms
Baseline: 81ms
Duration: 3.0s
```

This makes the history much easier to understand.

---

# 89. Incident Model

An incident contains:

```text
id
start
end
duration
severity
targets_affected
peak_latency
baseline_latency
packet_loss
diagnosis
confidence
```

---

# 90. Dashboard Incident Card

Example:

```text
🔴 SEVERE INCIDENT

18:27:13 → 18:27:16

Peak:
510 ms

Baseline:
81 ms

Affected:
Game Server

Router:
Healthy

Internet:
Healthy

Diagnosis:
Destination-specific instability

Confidence:
Medium
```

---

# 91. Accessibility

Support:

- keyboard navigation
- readable font sizes
- high contrast
- tooltips
- clear text labels
- color + text instead of color alone

Example:

```text
🔴 PROBLEM
```

not merely a red dot.

---

# 92. Localization

Start with:

```text
English
```

Architecture should allow:

```text
Swedish
German
French
```

through Qt translation files later.

---

# 93. Swedish UI Option

Because the application can be used in Swedish, labels could eventually include:

```text
Översikt
Liveövervakning
Diagnostik
Historik
Mål
Inställningar

Svarstid
Jitter
Paketförlust
Lagspikar
Nätverkshälsa
```

Keep internal code identifiers in English.

---

# 94. Important Technical Rule

Never use the UI to directly perform network measurements.

Bad:

```text
button → ping()
```

Better:

```text
Network Monitor
       ↓
Measurement
       ↓
Signal/Event
       ↓
UI
```

This keeps the application maintainable.

---

# 95. Configuration Example

Conceptual configuration:

```yaml
monitoring:
  gateway_interval_ms: 250
  internet_interval_ms: 500
  custom_interval_ms: 500
  timeout_ms: 1000

detection:
  spike_absolute_ms: 75
  spike_multiplier: 2.0
  rolling_window: 60

storage:
  retention_days: 30

notifications:
  enabled: true
  minimum_severity: severe
```

Actual implementation can use JSON/TOML/Pydantic settings instead.

---

# 96. First Targets

Default targets:

```text
Gateway:
auto-detected

Public:
1.1.1.1
8.8.8.8

Custom:
user-defined
```

Do not assume these public services are always the best representation of the game route.

---

# 97. What We Should Learn From the User's Graph

The uploaded example shows a pattern where latency appears relatively high and then repeatedly jumps upward.

The application should therefore emphasize:

```text
baseline
spike amplitude
spike duration
spike frequency
jitter
```

rather than only displaying the average ping.

The key question is:

> Does the router also spike at exactly the same time?

If yes:

```text
Local/Wi-Fi/router becomes much more suspicious.
```

If no:

```text
ISP/routing/destination becomes more suspicious.
```

That comparison should be a central feature of Internet Overwatch.

---

# 98. Final Product Concept

The finished application should feel like:

```text
Task Manager
        +
PingPlotter
        +
Network diagnostics
        +
Gaming latency monitor
        +
Historical incident recorder
```

but focused specifically on:

```text
"Why am I lagging?"
```

---

# 99. Final Dashboard Goal

The user should be able to open the application and immediately see:

```text
╔══════════════════════════════════════════════════════════╗
║ INTERNET OVERWATCH                                       ║
║                                                          ║
║                 82 / 100                                ║
║                 STABLE                                   ║
║                                                          ║
║   81ms          12ms          0.2%          4            ║
║   LATENCY       JITTER        LOSS          SPIKES       ║
║                                                          ║
║   ────────────────────────────────────────────────────   ║
║                 LIVE LATENCY                             ║
║                                                          ║
║        ╭╮                    ╭╮                         ║
║   ─────╯╰────────────────────╯╰──────                  ║
║                                                          ║
║   PC ── 2ms ── ROUTER ── 21ms ── INTERNET ── 81ms ─ GAME║
║   🟢             🟢               🟢              🟡     ║
║                                                          ║
║   RECENT INCIDENT                                        ║
║   🔴 18:27:13  510ms spike                              ║
║                                                          ║
║   LIKELY CAUSE                                           ║
║   Destination-specific instability                       ║
║   Confidence: Medium                                     ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
```

The application should make the user's network behavior **observable, measurable, and explainable**, not just display a ping number.
