# whoop-local

A single-file, **stdlib-only** Python tool that pairs with a **WHOOP 5.0 / MG ("Maverick")** band
**you own** over Bluetooth, decodes its data **locally**, and computes the health metrics — **no cloud,
no subscription, no account**. The more data it collects, the more it unlocks (a progressive ladder:
HR → strain/calories → zones → HRV → sleep → SpO₂ → recovery).

> Own-device, right-to-repair. Firmware is **read-only, never flashed**. Every number is a wellness
> estimate, **never medical**. Contains **no WHOOP code or assets** — "WHOOP" names the hardware only.

```bash
python3 whoop_dashboard.py --sim                    # no hardware: synthetic demo
python3 whoop_dashboard.py --live --offload --log night.json   # pair, stream, offload history
python3 whoop_dashboard.py capture.json             # full dashboard from a saved capture
python3 whoop_dashboard.py --selftest               # verify all sections
```

Live Bluetooth needs `bleak` (`pip install bleak`); **all decoding/analysis is pure stdlib**.

---

# Where every reading comes from

This is the whole point of the project: **nothing is invented.** Each field is either decoded straight
from the strap's own frames or computed with a published formula. Sources:

| Tag | Source | What it means |
|---|---|---|
| **[WHOOP-app]** | the official WHOOP Android app (decompiled, read-only) | the authoritative wire format |
| **[NOOP]** | [ryanbr/noop](https://github.com/ryanbr/noop) | RE project; offsets cross-checked vs the app |
| **[whoop-rs]** | [tanarchytan/whoop-rs](https://github.com/tanarchytan/whoop-rs) | Rust RE, **verified on real hardware** |
| **[GenieMax]** | [satayutata/geniemax-core](https://github.com/satayutata/geniemax-core) | published-formula engine |
| **[Published]** | peer-reviewed sports/medical formula | Keytel, Karvonen, Uth, Cole-Kripke… |
| **[Empirical]** | our own analysis harness in this file | HR-tracking channel finder, etc. |

## 1 · Raw fields — decoded straight from BLE frames
Offsets are **inner-relative** (frame byte − 8). `v18`/`v20`/`v21`/`v26` are historical record versions.

| Reading | How / offset | Source |
|---|---|---|
| Heart rate (live) | realtime type-40, `u8 @ inner 8` | [WHOOP-app] |
| Heart rate (history) | type-47 **v18**, `u8 @ inner 14` | [WHOOP-app] offset · [NOOP] value (corr 0.96) |
| Timestamp (unix) | `u32 @ inner 7` (hist) / `inner 2` (live) | [WHOOP-app] |
| **R-R intervals** | **v18** `count @ 15`, `u16 @ 16…` | **[whoop-rs]** (corrects an earlier "absent" call) |
| **Skin temperature** | **v18** `u16 @ 65`, **°C = raw / 100** | **[whoop-rs]** (validated: ~33 °C worn) |
| **SpO₂ %** | **v18** `u8 @ 74` — **WHOOP's own computed %, sleep-only** | **[whoop-rs]** |
| **Sleep-stage code** | **v18** `(byte @ 73 >> 4) & 3` | **[whoop-rs]** (code→name unverified) |
| Steps | **v18** `u16 @ 49` | [whoop-rs] |
| PPG waveform | **v26**: `frame[27:75]` = 24× i16 LE @ 24 Hz | [NOOP] (HR-locked, corr +0.907) |
| IMU accel X/Y/Z | **v21** type-43 R21, `i16 @ 20 / 220 / 420` (100 Hz) | [WHOOP-app] · [whoop-rs] (confirms it's IMU, not PPG) |
| IMU gyro X/Y/Z | **v21** `i16 @ 632 / 832 / 1032` | [WHOOP-app] |
| Optical buffer (v20) | 6 channels × 25 samples, 20-bit LE @ `[39,239,1305,1505,1727,1927]` | [whoop-rs] |
| Battery / firmware | COMMAND_RESPONSE `cmd@2 result@4 payload@5` | [WHOOP-app] |
| Events | type-48 (double-tap, wrist on/off, charging, boot…) | [WHOOP-app] |

## 2 · Computed metrics — published formulas over the raw fields

| Metric | Formula | Source |
|---|---|---|
| **Strain** (0–21) | `Σ 16/(e^((1−HRR)/0.15)+1)·dt / 691200` → 211-pt scale | [WHOOP-app `qh0`] |
| **Calories** (kJ) | Keytel-2005 per-sex + resting BMR, integrated over HR | [WHOOP-app] + [Published Keytel] |
| HR-reserve (Karvonen) | `(HR−rest)/(max−rest)`, 0 below 0.3 | [WHOOP-app] |
| HR zones Z1–Z5 | %HRR bands 0.5 / 0.6 / 0.7 / 0.8 / 0.9 | [Published Karvonen] |
| **HRV** (RMSSD) | beat-detect the v26 PPG → R-R → RMSSD, autocorrelation-anchored | [NOOP] DSP + our anchor fix |
| SDNN / SD1 / SD2 / Baevsky | from the R-R series | [Published] |
| HRmax / VO₂max | `208 − 0.7·age` / `15.3·HRmax/rest` | [Published Tanaka / Uth] |
| **Sleep staging** | Cole-Kripke actigraphy + z-score staging over IMU/HR/HRV | [GenieMax] algorithm |
| Recovery / Readiness | `100·Φ(0.55 z_HRV − 0.20 z_RHR − 0.10 z_RR + 0.15 z_sleep)` | [Published] — **our model, not WHOOP's** |

Full equations with constants: **[EQUATIONS.md](EQUATIONS.md)**.

## 3 · Local vs. cloud — what the strap does *not* give you

- **SpO₂:** WHOOP 5/MG has **no raw red/IR pair** on the wire (its v26 optical is a single AC-coupled
  wavelength — confirmed independently by [NOOP #548](https://github.com/ryanbr/noop/issues/548) and
  [whoop-rs](https://github.com/tanarchytan/whoop-rs)). Instead the strap **computes SpO₂ on-device
  during sleep** and stores the finished **% at v18 offset 74** — so we read WHOOP's own number, and it
  only appears in a **sleep** offload.
- **Recovery / Readiness:** the formula is implemented, but the z-scores need **≥7 nights** of personal
  baseline (WHOOP itself requires this) before the number is meaningful.
- **skin-temp / respiration in °C / br·min:** raw registers decode; °C is calibrated (`raw/100`),
  respiration rate still needs a calibrated envelope.

---

## Provenance rule

**Real captures, never invented offsets.** Every byte offset in this file is either lifted from the
decompiled app or verified against real hardware by one of the RE projects above. Where a value can't be
honestly derived (e.g. a fabricated SpO₂ from a single-wavelength signal), the tool **refuses to emit a
number** rather than guess — see the physiology gate in the R20/SpO₂ section.

## Credits

Built on the community's reverse-engineering: **[ryanbr/noop](https://github.com/ryanbr/noop)**,
**[tanarchytan/whoop-rs](https://github.com/tanarchytan/whoop-rs)**,
**[satayutata/geniemax-core](https://github.com/satayutata/geniemax-core)**. Thank you.

## Safety & scope

- Use **only** on a strap **you own**. Capture files hold your serial, a session token, and your MAC —
  they are git-ignored and should **never** be shared.
- Read-only w.r.t. the strap apart from the bonding handshake every BLE client performs; the only writes
  are the documented, non-destructive session/toggle commands. No firmware / reboot / DFU commands.
- Not a medical device. Numbers are wellness estimates.

## License

Non-commercial, own-device use. See [LICENSE](LICENSE).
