# WW934ZF / Chipsea BLE Advertisement Protocol

Wire format of the weight broadcast. Derived empirically — see
[REVERSE_ENGINEERING.md](REVERSE_ENGINEERING.md) for how it was obtained and
confirmed.

## Device identity

| Property | Value |
|---|---|
| Marketing name | Conair / Weight Watchers **WW934ZF** body scale |
| BLE module | Chipsea (advertises local name `Chipsea-BLE`) |
| Advertised service UUID | `0xFFF0` (Chipsea proprietary) |
| Address type | Public, **static** (does not rotate) |
| Manufacturer-data company ID | `0xA0CA` |

The scale is **dormant** when not in use — it transmits nothing. It begins
advertising only when a measurement starts (you step on it), emits the weight,
then goes silent again.

## Manufacturer-specific data payload

The payload below is the data carried under company ID `0xA0CA` (i.e. the bytes
after the 2-byte company identifier in the AD structure). 24 bytes:

```
offset  bytes   field        meaning
------  -----   -----        -------
[0]     1       header       constant 0xF3 (frame marker / sanity check)
[1]     1       state        0x01 = idle/empty, 0x03 = active measurement
[2]     1       type         0x04 = idle, 0x10 = active
[3:5]   2       weight       uint16, BIG-ENDIAN, units of 0.1 lb
[5:17]  12      reserved     zero on this model (no body-composition in adv)
[17]    1       checksum     varies with payload
[18:24] 6       device MAC   the scale's BD_ADDR, echoed in the payload
```

### Weight decode

```
weight_lb = int.from_bytes(payload[3:5], "big") * 0.1
```

Example: `payload[3:5] = 06 C0` → `0x06C0 = 1728` → **172.8 lb**.

### Worked frames (real captures)

```
idle:        F3 01 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 E8 CB ED 4E 23 0F
             └h └st└ty └────────────── weight=0 + reserved ──────┘ └ck └──── MAC ────┘

weigh-in:    F3 03 10 06 C0 00 00 00 00 00 00 00 00 00 00 00 00 4C E8 CB ED 4E 23 0F
             └h └st└ty └wt─┘                                   └ck
             state=0x03 active, weight=0x06C0=1728 → 172.8 lb, checksum=0x4C

stepped-off: F3 03 10 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8A E8 CB ED 4E 23 0F
             weight back to 0 as load leaves the scale
```

## Notes & limitations

- **Weight only.** Fat %, muscle %, bone %, BMI, and heart rate shown by the
  vendor app are **not** in the advertisement. They are derived over a GATT
  connection (service `0xFFF0` characteristics) and/or computed app-side from
  bio-impedance + a user profile. Capturing those requires a connected sniff, not
  a passive advertisement scan.
- **Units.** The captured unit broadcasts in **0.1 lb**. A scale configured to kg
  may differ; verify against a known weight before trusting the scale factor.
- **Checksum** (`[17]`) was not needed for decoding and was left unmodeled. It
  changes with the payload; treat readings as advisory and rely on the *settled*
  value (see [DESIGN.md](DESIGN.md)) rather than any single frame.
- **Ramp transients.** During step-on/step-off the weight field sweeps through
  intermediate values. Only a value that *holds* is the true reading — consumers
  must debounce (this tool waits for a value to persist ≥1.5 s).
