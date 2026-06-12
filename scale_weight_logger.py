"""Real-time weight logger for the Conair/WW WW934ZF (Chipsea BLE) scale.

Designed for unattended clinical monitoring (e.g. daily CHF weigh-ins). It scans
BLE advertisements via the host Bluetooth radio (no pairing/connection), decodes
the broadcast weight, and records every weigh-in. Reliability is the priority:

  * The local CSV is the source of truth. Each row is flushed + fsync'd so a crash
    or power loss cannot lose the last reading.
  * Desktop toast and Google Sheets append are BEST-EFFORT side channels. Any
    failure in them is caught and logged; it can never block or stop logging.
  * The scanner auto-restarts on Bluetooth-stack errors with backoff.
  * A weigh-in is finalized either by the scale's idle/zero beacon OR by an
    inactivity timeout, so the final weight is captured even if the scale sleeps
    without sending a closing beacon.

Protocol (reverse-engineered, confirmed against the app's own export):
    manufacturer_data[0xA0CA] = bytes:
        [0]   0xF3            protocol header (constant)
        [1]   state           0x01 idle/empty, 0x03 active measurement
        [2]   type            0x04 idle, 0x10 active
        [3:5] weight           big-endian uint16, units of 0.1 lb
        [17]  checksum
        [18:24] device MAC

PRIVACY: weight is health data. The CSV stays local. Prefer a non-identifying
``--patient`` label (an ID, not a name). Enabling Google Sheets sends readings to
the cloud — ensure the target sheet is private and acceptable for the data.

Setup:
    pip install bleak win11toast        # win11toast optional, for desktop toasts
    pip install gspread                 # optional, only for Google Sheets append

Usage:
    python scale_weight_logger.py
    python scale_weight_logger.py --patient CHF-01 --csv C:\\mon\\weights.csv
    python scale_weight_logger.py --sheet-id <ID> --sa-json C:\\mon\\service.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

# --- Protocol constants (from the reverse-engineered advertisement) ------------
SCALE_ADDRESS: str = "E8:CB:ED:4E:23:0F"  # public/static BD_ADDR of the captured unit
CHIPSEA_COMPANY_ID: int = 0xA0CA  # manufacturer-data company identifier the scale uses
HEADER_BYTE: int = 0xF3  # constant first byte; sanity-checks this is a scale frame
STATE_ACTIVE: int = 0x03  # b[1] during an active measurement
WEIGHT_OFFSET: int = 3  # b[3:5] holds the weight
WEIGHT_LSB_LB: float = 0.1  # each raw unit == 0.1 lb (0x06C0 -> 172.8 lb, confirmed)
MIN_PAYLOAD_LEN: int = 5  # need at least through b[4] to read the weight
KG_PER_LB: float = 0.45359237  # exact pounds -> kilograms factor

# Person attribution by weight. The two monitored people are well separated
# (~117 lb patient vs ~173 lb), so a midpoint threshold reliably attributes each
# reading. FOOTGUN: if two people weigh within a few lb of the threshold, or a
# third person uses the scale, this misattributes — it is a heuristic, not identity.
DEFAULT_THRESHOLD_LB: float = 155.0
DEFAULT_OVER_NAME: str = "Adult"  # label for the person at/above the threshold

# Behaviour tuning.
LOCK_SECONDS: float = 1.5  # a value must hold this long to count as the settled weight
SESSION_TIMEOUT_S: float = 8.0  # silence fallback to record/close a weigh-in
WATCHDOG_INTERVAL_S: float = 1.0  # how often the inactivity check runs
SCAN_RESTART_BACKOFF_S: float = 5.0  # delay before restarting after a stack error

# CHF fluid-retention alert thresholds (standard clinical rule of thumb).
DAILY_ALERT_LB: float = 2.0  # gain vs the most recent previous-day reading
WEEKLY_ALERT_LB: float = 5.0  # gain vs the lowest weight in the past week
WEEKLY_WINDOW_DAYS: int = 7

logger = logging.getLogger("scale")


@dataclass(frozen=True)
class WeightReading:
    """A single decoded weight broadcast."""

    weight_lb: float
    state: int
    is_active: bool


@dataclass(frozen=True)
class ReadingEvent:
    """An event handed to the I/O worker (decouples RF callback from slow I/O)."""

    event: str  # "live" | "stable" | "final"
    weight_lb: float
    weight_kg: float
    state: int
    address: str
    timestamp: str
    person: str
    dob: str


def decode_weight(payload: bytes) -> WeightReading | None:
    """Decode the scale's manufacturer-data payload into a weight reading.

    Args:
        payload: Bytes mapped to company id ``0xA0CA`` in the advertisement
            (everything after the 2-byte company identifier).

    Returns:
        A ``WeightReading`` if the payload is a valid scale frame, else ``None``.
    """
    # manufacturer_data is attacker-controllable RF input from any nearby device;
    # reject anything that is not the scale's known frame shape.
    if len(payload) < MIN_PAYLOAD_LEN or payload[0] != HEADER_BYTE:
        return None
    state = payload[1]
    weight_raw = int.from_bytes(payload[WEIGHT_OFFSET : WEIGHT_OFFSET + 2], "big")
    return WeightReading(
        weight_lb=round(weight_raw * WEIGHT_LSB_LB, 1),
        state=state,
        is_active=state == STATE_ACTIVE,
    )


def lb_to_kg(weight_lb: float) -> float:
    """Convert pounds to kilograms, rounded to 0.01 kg."""
    return round(weight_lb * KG_PER_LB, 2)


def resolve_person(
    weight_lb: float, threshold_lb: float, over_name: str, under_name: str
) -> str:
    """Attribute a reading to a person by weight.

    Heuristic only: assumes the monitored people are well separated in weight.
    Returns ``over_name`` if at/above ``threshold_lb``, else ``under_name``.
    """
    return over_name if weight_lb >= threshold_lb else under_name


class ToastNotifier:
    """Best-effort Windows desktop toast. Silently no-ops if unavailable."""

    def __init__(self, enabled: bool) -> None:
        self._toast = None
        if not enabled:
            return
        try:
            from win11toast import toast  # imported lazily; optional dependency

            self._toast = toast
        except ImportError:
            logger.warning("win11toast not installed; desktop notifications disabled")

    async def notify(
        self, weight_lb: float, weight_kg: float, person: str, alert: str = ""
    ) -> None:
        if self._toast is None:
            return
        if alert:
            title = f"⚠ CHF ALERT — {person}"
            body = f"{weight_lb:.1f} lb / {weight_kg:.2f} kg\n{alert}"
        else:
            title = f"Weight — {person}" if person else "Weight"
            body = f"{weight_lb:.1f} lb  /  {weight_kg:.2f} kg"
        try:
            # win11toast.toast spins its own event loop, so run it off the asyncio
            # thread to avoid "loop already running"; never let it raise upward.
            await asyncio.to_thread(self._toast, title, body)
        except Exception as exc:  # noqa: BLE001 - notification must never break logging
            logger.warning("toast failed: %s", exc)


class SheetAppender:
    """Best-effort append to a Google Sheet via gspread. Local CSV stays primary."""

    def __init__(self, sheet_id: str | None, sa_json: str | None) -> None:
        self._worksheet = None
        if not sheet_id:
            return
        try:
            import gspread  # imported lazily; optional dependency

            client = (
                gspread.service_account(filename=sa_json)
                if sa_json
                else gspread.service_account()
            )
            self._worksheet = client.open_by_key(sheet_id).sheet1
            logger.info("Google Sheets append enabled (sheet %s)", sheet_id)
        except Exception as exc:  # noqa: BLE001 - cloud is optional, never fatal
            logger.warning("Google Sheets disabled (init failed): %s", exc)

    async def append(self, row: list[str]) -> None:
        if self._worksheet is None:
            return
        try:
            await asyncio.to_thread(
                self._worksheet.append_row, row, value_input_option="USER_ENTERED"
            )
        except Exception as exc:  # noqa: BLE001 - never block local logging on network
            logger.warning("Sheets append failed (kept locally): %s", exc)


class CsvSink:
    """Append-only CSV with per-row flush + fsync for crash safety."""

    HEADER = [
        "timestamp_iso",
        "weight_lb",
        "weight_kg",
        "state_hex",
        "event",
        "address",
        "person",
        "dob",
        "alert",
    ]

    def __init__(self, path: Path) -> None:
        self._path = path
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(self.HEADER)

    def write(self, ev: ReadingEvent, alert: str) -> list[str]:
        row = [
            ev.timestamp,
            f"{ev.weight_lb:.1f}",
            f"{ev.weight_kg:.2f}",
            f"0x{ev.state:02X}",
            ev.event,
            ev.address,
            ev.person,
            ev.dob,
            alert,
        ]
        with self._path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(row)
            fh.flush()
            os.fsync(fh.fileno())  # guarantee the reading survives a crash/power loss
        return row


class HistoryTracker:
    """Per-person weight history for CHF daily/weekly gain detection.

    Loads prior readings from the CSV at startup so alerts survive restarts, then
    accumulates new readings in memory. Calendar-day comparisons use LOCAL time
    because patients weigh in on a local-morning schedule.
    """

    def __init__(self, csv_path: Path) -> None:
        self._history: dict[str, list[tuple[datetime, float]]] = {}
        self._load(csv_path)

    def _load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            return
        try:
            with csv_path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        when = datetime.fromisoformat(row["timestamp_iso"])
                        self._history.setdefault(row["person"], []).append(
                            (when, float(row["weight_lb"]))
                        )
                    except (KeyError, ValueError, TypeError):
                        continue  # skip malformed or pre-migration rows
        except OSError as exc:
            logger.warning("could not load weight history: %s", exc)

    def add(self, person: str, when: datetime, weight_lb: float) -> None:
        self._history.setdefault(person, []).append((when, weight_lb))

    def evaluate(self, person: str, when: datetime, weight_lb: float) -> str:
        """Return a CHF alert string if this reading breaches a gain threshold."""
        prior = self._history.get(person, [])
        if not prior:
            return ""
        local_day = when.astimezone().date()
        alerts: list[str] = []

        # Daily: vs the most recent reading on an earlier calendar day.
        earlier = [(d, w) for d, w in prior if d.astimezone().date() < local_day]
        if earlier:
            base_dt, base_w = max(earlier, key=lambda dw: dw[0])
            gain = weight_lb - base_w
            if gain >= DAILY_ALERT_LB:
                alerts.append(
                    f"+{gain:.1f} lb since {base_dt.astimezone():%m/%d} "
                    f"(>={DAILY_ALERT_LB:.0f}/day)"
                )

        # Weekly: vs the lowest weight in the past WEEKLY_WINDOW_DAYS.
        window_start = when - timedelta(days=WEEKLY_WINDOW_DAYS)
        recent = [w for d, w in prior if d >= window_start]
        if recent:
            gain = weight_lb - min(recent)
            if gain >= WEEKLY_ALERT_LB:
                alerts.append(
                    f"+{gain:.1f} lb over {WEEKLY_WINDOW_DAYS}d "
                    f"(>={WEEKLY_ALERT_LB:.0f}/wk)"
                )

        return " | ".join(alerts)


class WeightLogger:
    """Session state machine + event emission for the scale's broadcasts."""

    def __init__(
        self,
        match_any: bool,
        queue: asyncio.Queue[ReadingEvent],
        threshold_lb: float,
        over_name: str,
        under_name: str,
        over_dob: str,
        under_dob: str,
    ) -> None:
        self._match_any = match_any
        self._queue = queue
        self._threshold_lb = threshold_lb
        self._over_name = over_name
        self._under_name = under_name
        self._over_dob = over_dob
        self._under_dob = under_dob
        self._pending_lb: float | None = None  # latest non-zero value, still settling
        self._pending_since: float = 0.0  # loop time when _pending_lb first appeared
        self._locked: bool = False  # a reading already recorded for this weigh-in
        self._last_active: float = 0.0  # loop time of last non-zero frame

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _emit(self, event: str, weight_lb: float, state: int, addr: str) -> None:
        over = weight_lb >= self._threshold_lb
        self._queue.put_nowait(
            ReadingEvent(
                event=event,
                weight_lb=weight_lb,
                weight_kg=lb_to_kg(weight_lb),
                state=state,
                address=addr,
                timestamp=self._now_iso(),
                person=self._over_name if over else self._under_name,
                dob=self._over_dob if over else self._under_dob,
            )
        )

    def handle(self, device: BLEDevice, adv: AdvertisementData) -> None:
        """Detection callback. Fast: decode, track settling, emit one locked reading.

        Only a value that HOLDS for ``LOCK_SECONDS`` is recorded. Step-on/step-off
        ramp transients change too quickly to lock, so they are never logged and
        never trigger a notification — this prevents a ramp value from crossing the
        person threshold and being misattributed (e.g. a phantom ~150 lb reading
        pinned to the lighter person while the heavier user is still stepping on).
        """
        if not self._match_any and device.address.upper() != SCALE_ADDRESS:
            return
        payload = adv.manufacturer_data.get(CHIPSEA_COMPANY_ID)
        if payload is None:
            return
        reading = decode_weight(payload)
        if reading is None:
            return

        addr = device.address.upper()
        now = asyncio.get_event_loop().time()

        # Idle / stepped-off beacon ends the weigh-in (no emit here; the locked
        # reading, if any, was already recorded when the value settled).
        if reading.weight_lb == 0.0:
            self._reset()
            return

        self._last_active = now
        weight_lb = reading.weight_lb

        if self._locked:
            return  # already recorded this weigh-in; ignore steady rebroadcasts

        if weight_lb != self._pending_lb:
            # Still settling: value changed. Track it, but do not record/notify yet.
            self._pending_lb = weight_lb
            self._pending_since = now
            logger.debug("settling %.1f lb [%s]", weight_lb, addr)
            return

        # Same value persisting — has it held long enough to be the settled weight?
        if now - self._pending_since >= LOCK_SECONDS:
            self._lock_and_emit(reading.state, addr)

    def _lock_and_emit(self, state: int, addr: str) -> None:
        """Record the settled value as the one reading for this weigh-in."""
        if self._pending_lb is None:
            return
        self._emit("reading", self._pending_lb, state, addr)
        self._locked = True

    def _reset(self) -> None:
        """Clear state so the next weigh-in starts fresh."""
        self._pending_lb = None
        self._pending_since = 0.0
        self._locked = False

    async def watchdog(self) -> None:
        """Fallback: if the scale sleeps without a 0 beacon, record + close the session."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            if (
                self._pending_lb is not None
                and loop.time() - self._last_active > SESSION_TIMEOUT_S
            ):
                if not self._locked:
                    self._lock_and_emit(STATE_ACTIVE, SCALE_ADDRESS)
                self._reset()


async def io_worker(
    queue: asyncio.Queue[ReadingEvent],
    csv_sink: CsvSink,
    toaster: ToastNotifier,
    sheet: SheetAppender,
    history: HistoryTracker,
) -> None:
    """Consume events: one locked reading per weigh-in -> CHF check, CSV, toast, Sheets."""
    while True:
        ev = await queue.get()
        try:
            when = datetime.fromisoformat(ev.timestamp)
            alert = history.evaluate(ev.person, when, ev.weight_lb)
            history.add(ev.person, when, ev.weight_lb)  # add AFTER evaluating
            row = csv_sink.write(ev, alert)
            logger.log(
                logging.WARNING if alert else logging.INFO,
                "%-7s %6.1f lb / %6.2f kg  %-22s %s[%s]",
                ev.event,
                ev.weight_lb,
                ev.weight_kg,
                ev.person,
                f"ALERT: {alert}  " if alert else "",
                ev.address,
            )
            await toaster.notify(ev.weight_lb, ev.weight_kg, ev.person, alert)
            await sheet.append(row)
        except Exception as exc:  # noqa: BLE001 - a bad event must not kill the worker
            logger.error("event handling failed: %s", exc)
        finally:
            queue.task_done()


async def run(cfg: argparse.Namespace) -> None:
    """Wire up sinks and scan until interrupted, auto-restarting on stack errors."""
    queue: asyncio.Queue[ReadingEvent] = asyncio.Queue()
    wlogger = WeightLogger(
        cfg.match_any, queue, cfg.threshold, cfg.over_name, cfg.patient,
        cfg.over_dob, cfg.patient_dob,
    )
    history = HistoryTracker(cfg.csv)  # loads prior readings before the worker starts
    csv_sink = CsvSink(cfg.csv)
    toaster = ToastNotifier(not cfg.no_toast)
    sheet = SheetAppender(cfg.sheet_id, cfg.sa_json)

    worker = asyncio.create_task(io_worker(queue, csv_sink, toaster, sheet, history))
    watchdog = asyncio.create_task(wlogger.watchdog())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows: handled by KeyboardInterrupt in main()

    scope = "any Chipsea scale" if cfg.match_any else SCALE_ADDRESS
    logger.info("Scanning for %s -> %s (Ctrl+C to stop)", scope, cfg.csv)

    try:
        while not stop_event.is_set():
            try:
                scanner = BleakScanner(
                    detection_callback=wlogger.handle, scanning_mode="active"
                )
                await scanner.start()
                await stop_event.wait()
                await scanner.stop()
            except BleakError as exc:
                # Bluetooth stack hiccup (radio reset, adapter busy): back off + retry
                # rather than dying, so an unattended monitor self-heals.
                logger.error("scanner error: %s — restarting in %ss", exc, SCAN_RESTART_BACKOFF_S)
                await asyncio.sleep(SCAN_RESTART_BACKOFF_S)
    finally:
        worker.cancel()
        watchdog.cancel()
        logger.info("Stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time BLE weight logger (CHF).")
    parser.add_argument("--csv", type=Path, default=Path("scale_weight_log.csv"),
                        help="CSV log path (default: scale_weight_log.csv).")
    parser.add_argument("--log", type=Path, default=Path("scale_logger.log"),
                        help="Diagnostic log path (default: scale_logger.log).")
    parser.add_argument("--patient", default="CHF-01",
                        help="Label for readings BELOW the threshold (the CHF patient).")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_LB,
                        help=f"Weight (lb) at/above which a reading is attributed to "
                             f"--over-name (default: {DEFAULT_THRESHOLD_LB}).")
    parser.add_argument("--over-name", dest="over_name", default=DEFAULT_OVER_NAME,
                        help=f"Person at/above the threshold (default: {DEFAULT_OVER_NAME}).")
    parser.add_argument("--patient-dob", dest="patient_dob", default="",
                        help="DOB (YYYY-MM-DD) for the below-threshold person.")
    parser.add_argument("--over-dob", dest="over_dob", default="",
                        help="DOB (YYYY-MM-DD) for the at/above-threshold person.")
    parser.add_argument("--any", dest="match_any", action="store_true",
                        help="Log any Chipsea scale, not just the known MAC.")
    parser.add_argument("--no-toast", action="store_true",
                        help="Disable desktop notifications.")
    parser.add_argument("--sheet-id", default=None,
                        help="Google Sheet ID to append final readings to (optional).")
    parser.add_argument("--sa-json", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                        help="Service-account JSON for Google Sheets (optional).")
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args()


def main() -> None:
    cfg = parse_args()
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        RotatingFileHandler(cfg.log, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass  # Windows SIGINT path


if __name__ == "__main__":
    main()
