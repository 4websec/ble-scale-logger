# Design

Architecture and engineering rationale for `scale_weight_logger.py`. The protocol
it consumes is documented in [PROTOCOL.md](PROTOCOL.md).

## Goals

1. **Never miss or corrupt a reading.** This is used for clinical (CHF) weight
   monitoring, where a missed weigh-in or a phantom value has real consequences.
2. **Run unattended in the background**, cheaply, and self-heal.
3. **Degrade gracefully.** Optional features (notifications, cloud) must never be
   able to break core logging.

## Data flow

```
 BLE radio (host, via bleak)
      │  advertisement
      ▼
 WeightLogger.handle()        ── fast: decode + settle-detection (no I/O)
      │  ReadingEvent (only on a *settled* weigh-in)
      ▼
 asyncio.Queue               ── decouples RF callback from slow I/O
      │
      ▼
 io_worker()
   ├─ HistoryTracker.evaluate()   → CHF gain alert string
   ├─ CsvSink.write()             → append + flush + fsync   (SOURCE OF TRUTH)
   ├─ ToastNotifier.notify()      → desktop toast            (best-effort)
   └─ SheetAppender.append()      → Google Sheets row        (best-effort)

 watchdog()  ── finalizes a weigh-in if the scale sleeps without a 0-beacon
 run()       ── restarts the scanner with backoff on Bluetooth-stack errors
```

The detection callback does **no blocking I/O** — it decodes, updates a small
state machine, and enqueues at most one event per weigh-in. All file/network/UI
work happens in `io_worker`, off the RF path.

## Settle detection (why readings are clean)

A naïve logger that records every advertisement produces two failures, both
observed in early testing:

- **Phantom attribution.** As someone steps on, the weight *ramps up* through
  intermediate values. With a person-attribution threshold (below), a ramp value
  can cross into the other person's range — e.g. a transient `150.7 lb` logged as
  the wrong person while the real user climbs to `168 lb`.
- **Notification spam.** The settling oscillation fires a toast on every wiggle.

Fix: a value is recorded **only after it holds steady for `LOCK_SECONDS` (1.5 s)**.
Ramp/step-off transients change too fast to lock, so they are never logged and
never notified. Exactly **one** reading is emitted per weigh-in, attributed by the
*settled* weight. A `watchdog` provides a fallback: if the scale goes silent with
an un-locked pending value, the last value is recorded so a brief weigh-in isn't
lost.

State per weigh-in: `pending_lb`, `pending_since`, `locked`. Reset on the scale's
idle/zero beacon or on inactivity timeout.

## Person attribution

Two people share one scale. Attribution is a **weight threshold** (`--threshold`,
default 155 lb): at/above → `--over-name`, below → `--patient`. This works only
because the monitored people are well separated in weight.

> **Footgun (documented in code):** if two users weigh within a few lb of the
> threshold, or a third person uses the scale, attribution is wrong. It is a
> heuristic, not identity. For closely-matched weights, a different scheme
> (per-user scales, or a manual confirm) would be required.

## CHF weight-gain alerts

`HistoryTracker` loads prior readings from the CSV at startup (so alerts survive
restarts) and accumulates new ones in memory. On each reading it checks, per
person:

- **Daily:** gain ≥ `DAILY_ALERT_LB` (2 lb) vs. the most recent reading on an
  earlier **local** calendar day.
- **Weekly:** gain ≥ `WEEKLY_ALERT_LB` (5 lb) vs. the **lowest** weight in the past
  `WEEKLY_WINDOW_DAYS` (7) — the sensitive "dry-weight" comparison for fluid
  accumulation.

These mirror common CHF self-monitoring guidance (≈2–3 lb/day or ≈5 lb/week). A
breach writes the reason to the `alert` column, logs at `WARNING`, and raises a
distinct toast. Calendar-day math uses **local** time because patients weigh in on
a local-morning schedule.

> Not medical advice; thresholds are configurable constants, and a clinician
> should set the operative numbers.

## Reliability

| Concern | Mitigation |
|---|---|
| Crash / power loss | `flush()` + `os.fsync()` after every CSV row |
| Bluetooth stack drop | `run()` catches `BleakError`, backs off, restarts the scanner |
| Slow/failing toast or cloud | wrapped in `try/except` + `asyncio.to_thread`; never blocks logging |
| Scale sleeps without a 0-beacon | `watchdog` finalizes after `SESSION_TIMEOUT_S` of silence |
| Process death (unattended) | Task Scheduler restarts within 1 min (see Deployment) |
| Hostile/garbage RF input | `decode_weight` validates length + header before trusting bytes |

The ordering invariant: **CSV first, side channels after.** If the toast or Sheets
call throws, the row is already durably on disk.

## Resource profile

A BLE scan is handled by the OS Bluetooth stack; the Python process idles in the
asyncio loop awaiting callbacks. Measured: **~36 MB RSS, ~0 % CPU** at idle, brief
sub-second CPU on each advertisement. Suitable for 24/7 operation.

## Deployment

### Foreground / no window

```powershell
pythonw scale_weight_logger.py --patient "PATIENT-01" --csv C:\path\weights.csv
```

### Background service (Windows Task Scheduler)

Auto-starts at logon, auto-restarts within 1 minute of any crash, runs hidden.
BLE needs an interactive session for radio access, so it runs **as the logged-in
user at logon** — not a SYSTEM/session-0 service.

```powershell
$pythonw  = "C:\path\to\pythonw.exe"
$arg      = '"C:\path\scale_weight_logger.py" --patient "PATIENT-01" --threshold 155 ' +
            '--csv "C:\path\weights.csv" --log "C:\path\scale_logger.log"'
$action   = New-ScheduledTaskAction -Execute $pythonw -Argument $arg -WorkingDirectory "C:\path"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "ScaleWeightLogger" -Action $action -Trigger $trigger `
            -Settings $settings -RunLevel Limited
```

> **Gotcha:** scheduled tasks default their working directory to `System32`. Pass
> **absolute** paths for `--csv` and `--log` (and set `-WorkingDirectory`), or the
> non-admin task fails to open a relative log file and exits immediately.

## Code conventions

Single-file, fully type-annotated, stdlib + `bleak` only for the core. Optional
deps (`win11toast`, `gspread`) are imported lazily and disabled cleanly if absent.
Specific exceptions; no bare `except` except at explicit best-effort boundaries
(annotated `# noqa: BLE001`) where the rule is "a side channel must never crash
logging."
