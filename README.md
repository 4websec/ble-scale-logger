# BLE Weight-Scale Logger (Chipsea / Conair WW934ZF)

[![CI](https://github.com/4websec/ble-scale-logger/actions/workflows/ci.yml/badge.svg)](https://github.com/4websec/ble-scale-logger/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Real-time, passive Bluetooth Low Energy weight logger for the **Conair / Weight
Watchers WW934ZF** body scale (and other Chipsea-module scales). It listens for
the scale's BLE *advertisements* — no pairing, no connection, no vendor app — and
records each weigh-in to a CSV, with optional desktop notifications, multi-person
attribution, and clinical (CHF) weight-gain alerts.

The scale's weight protocol was reverse-engineered from a raw Bluetooth HCI
capture. See **[docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md)** for the
full teardown and **[docs/PROTOCOL.md](docs/PROTOCOL.md)** for the wire format.

> ⚠️ **Not a medical device.** This is a monitoring/automation tool. It does not
> diagnose or treat. Alert thresholds are general rules of thumb — defer to a
> clinician. See [Privacy & safety](#privacy--safety).

---

## Why advertisements?

When you step on the scale it **broadcasts the weight in its BLE advertisement
data** and then sleeps. A passive scanner therefore captures the reading with:

- **No pairing or bonding** — nothing to set up on the scale
- **No vendor app or cloud account**
- **No connection** — the scale never knows it's being read
- Works from any machine with a Bluetooth radio (the tool uses your host radio
  via [`bleak`](https://github.com/hbldh/bleak); Windows/Linux/macOS)

Body-composition fields (fat %, muscle, BMI, heart rate) are **not** in the
advertisement — those are computed over a GATT connection by the vendor app and
are out of scope here. This tool is weight-only by design.

---

## Features

| Feature | Notes |
|---|---|
| Passive advertisement capture | No pairing / no app / no connection |
| One clean reading per weigh-in | Records only a value that *settles* (≥1.5 s); ignores step-on/off ramp transients |
| lb + kg | Both recorded per reading |
| Multi-person attribution | Threshold split (e.g. ≥155 lb → person A, else person B) |
| CHF weight-gain alerts | Flags ≥2 lb/day or ≥5 lb/week vs. history; louder toast + `alert` column |
| Desktop notifications | Windows toast (optional, best-effort) |
| Google Sheets sync | Append each reading to a private Sheet (optional, best-effort) |
| Crash-safe logging | `flush + fsync` per row; CSV is the source of truth |
| Self-healing | Auto-restarts the scanner on Bluetooth-stack errors |
| Background service | Runs hidden via Task Scheduler; ~36 MB RAM, ~0 % idle CPU |

Reliability principle: the **local CSV is authoritative**. Notifications, Sheets,
and alerts are best-effort side channels — a failure in any of them is caught and
can never block or crash the logging path.

---

## Install

```bash
pip install -r requirements.txt        # core: bleak
pip install win11toast                  # optional: Windows desktop toasts
pip install gspread                     # optional: Google Sheets sync
```

Python 3.10+ required (`X | Y` type syntax, modern asyncio).

## Quick start

```bash
python scale_weight_logger.py
```

Leave it running and step on the scale. The scale only transmits while active, so
nothing is logged until a weigh-in — that's expected, not a hang.

### Multi-person + CHF monitoring example

```bash
python scale_weight_logger.py \
  --patient "PATIENT-01" --patient-dob 1970-01-01 \
  --over-name "Primary User" --over-dob 1985-01-01 \
  --threshold 155 \
  --csv weights.csv
```

Readings **≥ threshold** are attributed to `--over-name`, below to `--patient`.
(Pick a threshold that cleanly separates the two people's weights.)

### Key flags

| Flag | Default | Purpose |
|---|---|---|
| `--csv PATH` | `scale_weight_log.csv` | Authoritative log file |
| `--patient NAME` | `CHF-01` | Label for readings **below** the threshold |
| `--over-name NAME` | `Adult` | Label for readings **at/above** the threshold |
| `--threshold LB` | `155.0` | Attribution split point |
| `--patient-dob` / `--over-dob` | empty | DOB recorded per reading |
| `--sheet-id ID` / `--sa-json PATH` | off | Enable Google Sheets append |
| `--no-toast` | off | Disable desktop notifications |
| `--any` | off | Log any Chipsea scale, not just the configured MAC |
| `--verbose` | off | Debug logging (shows settling transients) |

> The default `SCALE_ADDRESS` in the script is one specific unit. Set your own
> scale's address (see [Reverse engineering](docs/REVERSE_ENGINEERING.md#1-find-the-scale))
> or run with `--any` to match any Chipsea scale nearby.

---

## Sample output

Console (placeholder names — real runs are weight-only health data and stay local):

```text
00:14:31 INFO Scanning for E8:CB:ED:4E:23:0F -> weights.csv (Ctrl+C to stop)
00:15:02 INFO reading   172.8 lb /  78.38 kg  Adult                  [E8:CB:ED:4E:23:0F]
08:03:11 INFO reading   116.4 lb /  52.80 kg  PATIENT-01             [E8:CB:ED:4E:23:0F]
08:03:11 WARNING reading 119.0 lb / 53.98 kg  PATIENT-01  ALERT: +2.6 lb since 06/11 (>=2/day)  [E8:CB:ED:4E:23:0F]
```

CSV:

```csv
timestamp_iso,weight_lb,weight_kg,state_hex,event,address,person,dob,alert
2026-06-12T05:15:02+00:00,172.8,78.38,0x03,reading,E8:CB:ED:4E:23:0F,Adult,,
2026-06-12T13:03:11+00:00,116.4,52.80,0x03,reading,E8:CB:ED:4E:23:0F,PATIENT-01,1970-01-01,
2026-06-13T13:03:11+00:00,119.0,53.98,0x03,reading,E8:CB:ED:4E:23:0F,PATIENT-01,1970-01-01,+2.6 lb since 06/11 (>=2/day)
```

## CSV format

```
timestamp_iso, weight_lb, weight_kg, state_hex, event, address, person, dob, alert
```

`event` is `reading` for the settled weigh-in (the watchdog may also emit one on
scale sleep). `alert` is populated only when a CHF gain threshold is breached.

---

## Run as a background service

See **[docs/DESIGN.md → Deployment](docs/DESIGN.md#deployment)** for the Windows
Task Scheduler setup (auto-start at logon, auto-restart on crash, runs hidden).

---

## Repository layout

```
scale_weight_logger.py        the logger (single file, typed, stdlib + bleak)
requirements.txt
docs/
  REVERSE_ENGINEERING.md      how the protocol was captured and decoded
  PROTOCOL.md                 the advertisement wire format
  DESIGN.md                   architecture, reliability, deployment
LICENSE                       MIT
.gitignore                    excludes all PHI / captures
```

---

## Privacy & safety

- **PHI never enters the repo.** Patient names, DOBs, and weights live only in
  local run output, which is git-ignored. Real identities are passed as CLI args
  (in your service config), never hard-coded in source.
- **Health data is sensitive.** If you enable Google Sheets, ensure the target
  sheet is private and that cloud storage of the data is acceptable.
- **Bluetooth addresses** of unrelated nearby devices may appear in raw HCI
  captures used during reverse engineering — those captures are git-ignored too.
- This tool reads a device **you own**. Don't use it to capture scales you don't.

---

## License

MIT — see [LICENSE](LICENSE).
