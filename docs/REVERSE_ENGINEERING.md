# Reverse Engineering the WW934ZF Weight Protocol

How the scale's BLE weight broadcast was captured and decoded, end to end. The
result is documented in [PROTOCOL.md](PROTOCOL.md).

## Goal

Read the scale's weight on a PC **without** the vendor app, without pairing, and
without an over-the-air sniffer dongle — using only an Android phone and `adb` to
capture, then `tshark` to analyze.

## Tooling

- Android phone (the scale's broadcasts only need to be *received* by something)
- `adb` (Android platform-tools)
- Wireshark / `tshark`
- A target scale you own (Conair / WW WW934ZF)

---

## 1. Find the scale

A Bluetooth HCI snoop log records every packet the phone's radio sends/receives,
including advertisement reports — so the phone just needs to be **scanning** while
the scale broadcasts. No vendor app required: Android's own *Settings → Bluetooth
→ Pair new device* screen keeps the radio actively scanning.

Enable the capture:

1. **Developer options → Enable Bluetooth HCI snoop log → Enabled / "All"** (the
   "Full" mode keeps payloads; "Filtered" strips them).
2. Toggle Bluetooth **off/on** so the log starts clean.
3. Open the Bluetooth scan screen and **step on the scale** to make it broadcast.

Confirm snoop logging is active:

```bash
adb shell "dumpsys bluetooth_manager | grep -iE 'snoop|enabled'"
# -> enabled: true ... sSnoopLogSettingAtEnable = FULL
```

---

## 2. Pull the capture

Modern Samsung builds don't drop the snoop log on shared storage; it lives at
`/data/misc/bluetooth/logs/` (root-only). The reliable extraction is via a
bugreport, whose privileged `dumpstate` bundles the file:

```bash
adb bugreport bugreport.zip
# btsnoop_hci.log is inside, e.g. at:  FS/data/log/bt/btsnoop_hci.log
```

Extract `btsnoop_hci.log` from the zip. It's standard `btsnoop` format and opens
natively in Wireshark.

---

## 3. Identify the device

List advertised local names in the capture:

```bash
tshark -r btsnoop_hci.log \
  -Y 'btcommon.eir_ad.entry.device_name' \
  -T fields -e btcommon.eir_ad.entry.device_name | sort -u
# -> Chipsea-BLE   <-- the scale's BLE module
```

Get its address and full advertisement:

```bash
tshark -r btsnoop_hci.log \
  -Y 'btcommon.eir_ad.entry.device_name == "Chipsea-BLE"' \
  -O bthci_evt,btcommon
```

This reveals a **public, static** address (`E8:CB:ED:4E:23:0F` for the captured
unit, OUI = Chipsea Technology) and service UUID `0xFFF0`. A static address is
convenient — you can filter on it forever.

---

## 4. Extract the weight-bearing frames

Dump all advertisement payloads from the scale's address:

```bash
tshark -r btsnoop_hci.log \
  -Y 'bthci_evt.bd_addr == e8:cb:ed:4e:23:0f && btcommon.eir_ad.entry.data' \
  -T fields -e frame.time_relative \
           -e btcommon.eir_ad.entry.company_id \
           -e btcommon.eir_ad.entry.data
```

This surfaces a manufacturer-data record (company ID `0xA0CA`) with two payload
shapes:

```
idle (state byte = 0x01):
  F3 01 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 E8 CB ED 4E 23 0F

active weigh-in (state byte = 0x03):
  F3 03 10 06 C0 00 00 00 00 00 00 00 00 00 00 00 00 4C E8 CB ED 4E 23 0F
```

> Watch for **byte-shift traps** when hand-counting hex. An early decode attempt
> mis-aligned `[3:4]` and read `0x00C0 = 192` instead of `0x06C0 = 1728`. Always
> index the bytes programmatically and print them with offsets before trusting a
> field position.

---

## 5. Decode

Diffing idle vs. active frames isolates the changing fields:

| Offset | Idle | Active | Inference |
|---|---|---|---|
| `[0]` | `F3` | `F3` | constant header |
| `[1]` | `01` | `03` | state: idle vs. active |
| `[2]` | `04` | `10` | type: idle vs. active |
| `[3:5]` | `0000` | `06C0` | **weight** (big-endian uint16) |
| `[5:17]` | zero | zero | reserved (no body-comp in adv) |
| `[17]` | `00` | `4C` | checksum |
| `[18:24]` | MAC | MAC | device address echoed |

Candidate scalings for `0x06C0 = 1728`:

- `× 0.1 lb` → **172.8 lb**
- `× 0.01 kg` → 17.28 kg

---

## 6. Confirm against ground truth

The vendor app exports a CSV of readings. The weigh-in that coincided with the
capture appears there as **172.8 lb** — an exact match for the `× 0.1 lb`
interpretation. The kg hypothesis (17.28 kg) is ruled out.

```
BLE advertisement  0x06C0 = 1728  → 172.8 lb
Vendor app export                 → 172.8 lb   ✅
```

Units are therefore **big-endian uint16, 0.1 lb per LSB**. The app's body-
composition columns (fat %, muscle, BMI, HR) have **no** counterpart in the
advertisement (those bytes are always zero), confirming they ride a GATT
connection — out of scope for a passive scan.

---

## 7. Live verification

A standalone `bleak` scanner on the PC's own radio, filtered to the scale's
address and company ID `0xA0CA`, reproduces the decode in real time — no phone in
the loop. That scanner became [`scale_weight_logger.py`](../scale_weight_logger.py).

## Takeaways

- BLE scales often broadcast weight in the **advertisement** — capturing it can be
  as cheap as an HCI snoop log, no OTA sniffer hardware needed.
- A vendor data export is invaluable **ground truth** for locking a scale factor.
- Decode against **programmatically indexed** bytes; never eyeball hex offsets.
