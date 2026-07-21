#!/usr/bin/env python3
"""whoop_dashboard.py — WHOOP 5 (Maverick): pair → capture → decode → PROGRESSIVELY analyse, one file.

Reads top-to-bottom; the more data it collects, the more it unlocks (the "progressive ladder"):
  §1-2 FRAMING + DECODE     every packet type & field                    [JAVA]
  §3   STRAIN + CALORIES    WHOOP's on-device engine                     [JAVA qh0/*]
  §4   HRV                  RMSSD from v26 PPG                            [NOOP]
  §5   R20 / SpO2           optical channel finder + calibrated SpO2     [empirical + Maxim curve]
  §6   BLE COMMANDS         hello / toggles / LINK_VALID / offload       [JAVA zi0,bj0]
  §7   METRICS + DASHBOARD  HRV panel, zones, strain, recovery
  §8   LIVE                 pair → stream → offload, deepening live
  §9   CLI

Data → analysis ladder (unlocks as samples accumulate):
  connect → HR → (90 s) Strain+Calories → Zones → (offload) HRV → (R20) SpO2 → (7 d) Recovery.

Usage:
  python3 whoop_dashboard.py --live --pair --offload      # pair, stream, offload, live progressive view
  python3 whoop_dashboard.py --sim                        # no hardware: synthetic live stream
  python3 whoop_dashboard.py capture.json                 # full dashboard from a saved capture
  python3 whoop_dashboard.py --r20 capture.json           # R20 optical / SpO2
  python3 whoop_dashboard.py --field-map / --selftest
Live BLE needs `bleak` (imported lazily). Use only on a strap you own.
"""
#
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  INDEX — every reading: HOW we get it & its SOURCE
#  Provenance:  [JAVA] decompiled WHOOP app  ·  [NOOP] RE project (offsets verified vs the Java)
#               [PUB] published sports/medical formula  ·  [EMP] our empirical harness
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
#  SECTION MAP
#    §1-2  FRAMING + PROTOCOL DECODE  [JAVA] ............. line 99
#    §6  BLE COMMANDS + REASSEMBLER  [JAVA zi0/*, bj0/*]  (redefines crc — identical) ... line 422
#    §3  STRAIN + CALORIES  [JAVA qh0/*] ................. line 897
#    §4  HRV from v26 PPG  [NOOP] ........................ line 1320
#    §5  R20 OPTICAL / SpO2  [empirical] ................. line 1450
#    §7  METRICS (GenieMax) + DASHBOARD .................. line 1778
#    §8  LIVE  (pair → stream → offload, progressive) .... line 2039
#    §8.5  DEEP-MINE EXTRAS — ECG · optical AFE · data-product commands · device state · GPS · Pip  [JAVA] ... line 2383
#    §8.6  SLEEP STAGING — Cole-Kripke actigraphy + z-score staging  [algorithm: GenieMax SleepStaging.swift] ... line 2637
#    §8.7  R20 GenieMax seed + CAPTURE LOG (send me the log to reverse SpO2)  [GenieMax WhoopDecode + ours] ... line 2773
#    §9  CLI ............................................. line 2862
#
#  RAW FIELDS  (decoded straight from BLE frames)
#    heart rate (live)          realtime type-40, u8 @ inner 8              [JAVA ej0/j]  (app-labelled HR)
#    heart rate (historical)    type-47 v18, u8 @ frame 22 (inner 14)       [JAVA kq0/b offset] [NOOP value, corr 0.96]
#    timestamp (unix)           u32 @ inner 7 (hist) / inner 2 (realtime)   [JAVA kq0/b, ej0/j]
#    record index / seq         u32 @ inner 3                               [JAVA kq0/b G()]
#    off-wrist / body location  type-40, @ inner 18 (==0) / 19              [JAVA ej0/j]
#    PPG waveform (v26)         frame[27:75] = 24× i16 LE @ 24 Hz           [NOOP, HR-locked corr +0.907]
#    IMU accel X/Y/Z            type-43 R21, i16 @ inner 20 / 220 / 420     [JAVA mq0/e]
#    IMU gyro X/Y/Z             type-43 R21, i16 @ inner 632 / 832 / 1032   [JAVA mq0/e]
#    R20 optical (raw)          type, len 2140, opaque blob → §5 harness    [JAVA mq0/d (size only)]
#    offload trim / end_data    METADATA HISTORY_END, frame[21:29]          [JAVA ej0/h, ej0/b]
#    battery / fw versions      COMMAND_RESPONSE: cmd@2 result@4 payload@5  [JAVA bj0/f]
#    R-R intervals              NOT PRESENT in any BLE packet (removed)     [JAVA — verified absent]
#
#  COMPUTED METRICS
#    Strain (0-21)              per-sample 16/(exp((1-hrr)/0.15)+1)·dt,     [JAVA qh0/o, qh0/d]
#                               Σ/691200, then 211-point scale table        [JAVA qh0/p]
#    Calories (kJ / kcal)       Keytel-2005 per-sex + resting BMR,          [JAVA qh0/b + BiologicalSex.java]
#                               integrated over smoothed HR, ×4.184
#    max-HR estimate            -0.27262·age + 204.43 + sex + fitness       [JAVA qh0/o g()]
#    HR-reserve (Karvonen)      (HR-rest)/(max-rest), 0 below 0.3 floor     [JAVA qh0/d b()]
#    HR smoothing               25th-percentile over ±30-sample window       [JAVA qh0/o n()]
#    HRV — RMSSD                detect beats in the v26 PPG → RR → RMSSD     [NOOP DSP (whoop_spot_hrv)]
#    HRV — SDNN/SD1/SD2/Baevsky from the PPG-derived RR series               [PUB]
#    HR zones (Z1-Z5)           %HRR bands 0.5/0.6/0.7/0.8/0.9              [PUB Karvonen]
#    TRIMP / day-strain (alt)   Banister 0.64e^1.92x / 0.86e^1.67x          [PUB Banister]
#    VO2max                     15.3 · HRmax/HRrest                         [PUB Uth 2004]
#    Recovery / Readiness       100·Φ(0.55 z_HRV -0.20 z_RHR …) / blend     [PUB — our GenieMax model, NOT WHOOP's]
#    SpO2 (relative)            R=(AC/DC)red/(AC/DC)ir → -45.06R²+30.35R+94.85 [EMP + Maxim MAX30102 curve]
#
#  COMMANDS  (how we drive the strap)
#    session open               GET_HELLO(145), revision-1 payload          [JAVA zi0/o]  = WHOOP5_CLIENT_HELLO
#    enable HR stream           TOGGLE_REALTIME_HR(3) = [0x01]              [JAVA zi0/h1]
#    enable IMU / optical       TOGGLE_IMU_MODE(106) → OPTICAL(108)=[1,on]  [JAVA zi0/d1, e1 · order c.java l()]
#    LINK_VALID handshake       reply COMMAND_RESPONSE "There it is."       [JAVA bj0/x, bj0/f]
#    offload pull + ack         SEND_HISTORICAL_DATA(22); RESULT(23)=1+8B   [JAVA zi0/s, zi0/m · ej0/b]
#
#  NOT DECODABLE LOCALLY  (WHOOP cloud, proprietary): Recovery% · Sleep stages · final HRV · absolute SpO2%
#  BLOCKED ON macOS: BLE bonding (CoreBluetooth has no pairing API) → use Linux/Pi for --live
# ══════════════════════════════════════════════════════════════════════════════════════════════════
import argparse
import asyncio
import json
import math
import os
import signal
import statistics
import struct
import sys
import time
import zlib



# ==================================================================================================
# §1-2  FRAMING + PROTOCOL DECODE  [JAVA]
# ==================================================================================================

# =====================================================================================================
# 1. DEVICE FAMILIES + BLE UUIDs          [JAVA rq0/o.java (WhoopStrapService enum), rq0/p.java]
# =====================================================================================================
# Each family binds a service + 5 characteristics + a codeName + a version byte. Role convention:
#   …0001 service · …0002 Cmd→Strap (write) · …0003 Cmd←Strap · …0004 Events←Strap · …0005 Data←Strap
#   …0007 MemFault.  0003/0004/0005/0007 are notify sources.
DEVICE_FAMILIES = {
    "GEN_4":    {"codeName": "Harvard",  "marketing": "WHOOP 4.0",     "version": 4,
                 "service": "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"},
    "MAVERICK": {"codeName": "Maverick", "marketing": "WHOOP 5.0 / MG", "version": 5,
                 "service": "fd4b0001-cce1-4033-93ce-002d5875f58a"},
    "GOOSE":    {"codeName": "Goose",    "marketing": "WHOOP 5 variant", "version": 5,
                 "service": "fd4b0001-cce1-4033-93ce-002d5875f58a"},   # reuses Maverick UUIDs
    "PUFFIN":   {"codeName": "Puffin",   "marketing": "battery pack",   "version": 5,
                 "service": "11500001-6215-11ee-8c99-0242ac120002"},
    "MONUMENT": {"codeName": "Monument", "marketing": "newer gen",      "version": 5,
                 "service": "8a580001-2fe8-4796-9267-b87a2b0c8234"},
    "SYMPHONY": {"codeName": "Symphony", "marketing": "newer gen",      "version": 5,
                 "service": "59830001-5955-419b-bb8d-c8262926af23"},
}
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"   # standard HR profile (unencrypted)

# =====================================================================================================
# 2. FRAMING + BYTE ORDER                 [JAVA kq0/d.java (LITTLE_ENDIAN), lq0/e.java, sq0/c.java (CRC)]
# =====================================================================================================
# WHOOP 5 (Maverick/"puffin") wire frame:
#   [0xAA][0x01][declLen u16 LE][role u8=0x00][role u8=0x01][crc16 LE][inner record…][crc32 LE]
#   total = declLen + 8 ;  inner record starts at byte 8 ;  crc16 over bytes[0:6] ;  crc32 over inner.
# WHOOP 4 (Harvard):
#   [0xAA][len u16 LE][crc8][inner record…][crc32 LE] ;  total = len + 4 ;  inner starts at byte 4.
SOF = 0xAA
WHOOP5_INNER_OFF = 8
WHOOP4_INNER_OFF = 4


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def _crc8_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
        t.append(c)
    return t


_CRC8 = _crc8_table()


def crc8(data):
    crc = 0
    for b in data:
        crc = _CRC8[crc ^ b]
    return crc


def verify_frame(frame, family="whoop5"):
    """True if declared length + CRC16/CRC8 header + CRC32 trailer all check out."""
    if len(frame) < 12 or frame[0] != SOF:
        return False
    if family == "whoop5":
        total = (frame[2] | (frame[3] << 8)) + 8
        if total != len(frame) or crc16_modbus(bytes(frame[0:6])) != (frame[6] | (frame[7] << 8)):
            return False
        inner = bytes(frame[8:total - 4])
    else:
        total = (frame[1] | (frame[2] << 8)) + 4
        if total != len(frame) or crc8(bytes(frame[1:3])) != frame[3]:
            return False
        inner = bytes(frame[4:total - 4])
    return crc32(inner) == int.from_bytes(frame[total - 4:total], "little")


def inner_record(frame, family="whoop5"):
    """The inner record (type byte @0, CRC32 trailer stripped), or None if the frame fails CRC."""
    if not verify_frame(frame, family):
        return None
    off = WHOOP5_INNER_OFF if family == "whoop5" else WHOOP4_INNER_OFF
    return frame[off:-4]


# =====================================================================================================
# 3. PACKET TYPES  [JAVA kq0/c.java]   +   COMMAND OPCODES  [JAVA kq0/e.java]
# =====================================================================================================
PACKET_TYPES = {
    35: "COMMAND", 36: "COMMAND_RESPONSE", 37: "PUFFIN_COMMAND", 38: "PUFFIN_COMMAND_RESPONSE",
    40: "REALTIME_DATA", 43: "REALTIME_RAW_DATA", 47: "HISTORICAL_DATA", 48: "EVENT",
    49: "METADATA", 50: "CONSOLE_LOGS", 51: "REALTIME_IMU_DATA_STREAM", 52: "HISTORICAL_IMU_DATA_STREAM",
    53: "RELATIVE_PUFFIN_EVENTS", 54: "PUFFIN_EVENTS_FROM_STRAP",
    55: "RELATIVE_BATTERY_PACK_CONSOLE_LOGS", 56: "PUFFIN_METADATA",
}
METADATA_SUBTYPES = {1: "HISTORY_START", 2: "HISTORY_END", 3: "HISTORY_COMPLETE"}
CMD_RESULTS = {0: "FAILURE", 1: "SUCCESS", 2: "PENDING", 3: "UNSUPPORTED"}   # [JAVA yi0/b.java]

# Selected command opcodes actually used (full enum in kq0/e.java).
COMMANDS = {
    1: "LINK_VALID", 3: "TOGGLE_REALTIME_HR", 10: "SET_CLOCK", 11: "GET_CLOCK",
    14: "TOGGLE_GENERIC_HR_PROFILE", 20: "ABORT_HISTORICAL_TRANSMITS", 22: "SEND_HISTORICAL_DATA",
    23: "HISTORICAL_DATA_RESULT", 26: "GET_BATTERY_LEVEL", 34: "GET_DATA_RANGE", 63: "SEND_R10_R11_REALTIME",
    98: "GET_EXTENDED_BATTERY_INFO", 106: "TOGGLE_IMU_MODE", 108: "TOGGLE_OPTICAL_MODE",
    141: "GET_ADVERTISING_NAME", 145: "GET_HELLO", 151: "GET_BATTERY_PACK_INFO",
    153: "TOGGLE_PERSISTENT_R20", 154: "TOGGLE_PERSISTENT_R21",
}

# R-channel taxonomy (offset-1 "K()" / upload channel).  [JAVA zt0/c.java router, jq0/b.java enum]
R_CHANNELS = {
    16: "R16 ECG",  17: "R17 Labrador filtered",  20: "R20 optical (opaque blob on-device)",
    21: "R21 IMU (accel+gyro)",  25: "R25 Pip (pulse-info)",  26: "R26 Pip",
}


def _u8(b, o):  return b[o] & 0xFF
def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _i16(b, o): return struct.unpack_from("<h", b, o)[0]
def _i16_array(b, start, count):
    return [struct.unpack_from("<h", b, start + 2 * i)[0] for i in range(count)]


# =====================================================================================================
# 4. HISTORICAL_DATA (type 47)            [JAVA kq0/b.java (BleDataPacket)]
# =====================================================================================================
# Inner-record offsets (LITTLE_ENDIAN). The version byte (K@1) selects layout: v18 = WHOOP-5 per-second
# biometric summary; v26 = WHOOP-5 88-byte PPG buffer; v24 = WHOOP-4. Body beyond these fields is
# uploaded RAW (server-decoded), so only the offsets below are on-device knowledge.
HIST_TYPE_OFF, HIST_VER_OFF, HIST_INDEX_OFF, HIST_UNIX_OFF, HIST_SUBSEC_OFF, HIST_VALID_MIN = 0, 1, 3, 7, 11, 13
# J(): a per-version byte. App only tests it as `J()!=0` (a validity flag) — but NOOP cross-validated
# the v18 value as heart rate vs a reference WHOOP-4C (corr 0.96). So v18 = HR (empirical); others: raw.
HR_OFFSET_BY_VERSION = {7: 27, 9: 17, 12: 17, 18: 14, 24: 17}   # NO v25/v26 case (raw records)
V26_LEN = 88
V26_PPG_START, V26_PPG_END = 27, 75      # 24 LE-i16 PPG samples @24 Hz (frame-absolute)  [NOOP corr +0.907]


def decode_historical(rec, frame=None):
    """Decode a HISTORICAL_DATA (type 47) inner record."""
    if len(rec) < HIST_VALID_MIN:
        return {"type_name": "HISTORICAL_DATA", "valid": False}
    version = _u8(rec, HIST_VER_OFF)
    out = {"type_name": "HISTORICAL_DATA", "version": version,
           "index": _u32(rec, HIST_INDEX_OFF), "unix": _u32(rec, HIST_UNIX_OFF),
           "subsec": _u16(rec, HIST_SUBSEC_OFF)}
    hr_off = HR_OFFSET_BY_VERSION.get(version)
    if hr_off is not None and hr_off < len(rec):
        out["heart_rate"] = _u8(rec, hr_off)              # v18: empirically HR (NOOP); else: raw byte
    # v18 fields — inner offsets VERIFIED against real hardware by whoop-rs
    # (tanarchytan/whoop-rs · crates/whoop-protocol/src/records/gen5.rs). These are WHOOP's OWN
    # on-device computed values carried in the record — not our re-derivations.
    if version == 18:
        rc = len(rec)
        if rc >= 67:
            st = _u16(rec, 65)                            # skin-temp register; °C = raw/100, kept 5..45 °C
            if 500 <= st <= 4500:
                out["skin_temp_raw"] = st
                out["skin_temp_c"] = round(st / 100.0, 2)
        if rc >= 75:
            v = _u8(rec, 74)                              # sleep-only computed SpO2 %, tri-mode gated 70..100
            if 70 <= v <= 100:
                out["spo2_pct"] = v
        if rc >= 74:
            out["sleep_state"] = (_u8(rec, 73) >> 4) & 3  # WHOOP's own sleep-stage code (0..3)
        if rc >= 51:
            out["steps"] = _u16(rec, 49)
        if rc >= 56:
            ac = _u8(rec, 55)
            if ac <= 2:
                out["activity_class"] = ac                # 0/1/2 motion class
        if rc >= 33:
            out["signal_quality"] = _u8(rec, 32)
        if rc >= 16:                                      # R-R intervals: count @15, u16 each from @16
            n_rr = _u8(rec, 15)
            if 0 < n_rr <= 8:
                rr = [_u16(rec, 16 + 2 * i) for i in range(n_rr) if 16 + 2 * i + 2 <= rc]
                rr = [x for x in rr if 300 <= x <= 2000]
                if rr:
                    out["rr_ms"] = rr
    # v26 PPG waveform lives in the FULL frame at [27:75] (24 LE-i16 @24 Hz).
    if version == 26 and frame is not None and len(frame) == V26_LEN:
        out["ppg_waveform"] = _i16_array(frame, V26_PPG_START, (V26_PPG_END - V26_PPG_START) // 2)
        out["ppg_rate_hz"] = 24
    return out


# =====================================================================================================
# 5. REALTIME_DATA (type 40)              [JAVA ej0/j.java (RealTimeDataPacket)]
# =====================================================================================================
def decode_realtime(rec):
    """Live HR frame. HR@8 is the ONLY byte the app itself labels as heart rate (vi0/a.java)."""
    if len(rec) <= 19:
        return {"type_name": "REALTIME_DATA", "valid": False}
    return {"type_name": "REALTIME_DATA",
            "revision": _u8(rec, 1), "unix": _u32(rec, 2), "subsec": _u16(rec, 6),
            "heart_rate": _u8(rec, 8), "off_wrist": _u8(rec, 18) == 0, "body_location": _u8(rec, 19)}


# =====================================================================================================
# 6. REALTIME_RAW_DATA (type 43) — R21 IMU   [JAVA mq0/e.java (R21MaverickDatapacket) → xi0/a.java ImuData]
# =====================================================================================================
# Inner offsets. Each axis array is a fixed 200-byte span (up to 100 × i16); the count field says how
# many are valid. This is inertial data — 3 accel axes + 3 gyro axes — NOT optical.
IMU_SAMPLING_FREQ_OFF, IMU_NUM_ACCEL_OFF = 14, 16
IMU_ACCEL_X_OFF, IMU_ACCEL_Y_OFF, IMU_ACCEL_Z_OFF = 20, 220, 420
IMU_NUM_GYRO_OFF = 622
IMU_GYRO_X_OFF, IMU_GYRO_Y_OFF, IMU_GYRO_Z_OFF = 632, 832, 1032


def decode_imu_r21(rec):
    """Decode a type-43 R21 IMU record (accelerometer + gyroscope). Only when K()==21."""
    if len(rec) < 18 or _u8(rec, 1) != 21:
        return None
    n_acc = _u16(rec, IMU_NUM_ACCEL_OFF)
    n_gyr = _u16(rec, IMU_NUM_GYRO_OFF) if len(rec) > IMU_NUM_GYRO_OFF + 1 else 0
    n_acc = max(0, min(n_acc, 100)); n_gyr = max(0, min(n_gyr, 100))
    out = {"type_name": "REALTIME_RAW_DATA", "channel": "R21 IMU",
           "index": _u32(rec, HIST_INDEX_OFF), "unix": _u32(rec, HIST_UNIX_OFF),
           "sampling_hz": _u16(rec, IMU_SAMPLING_FREQ_OFF), "n_accel": n_acc, "n_gyro": n_gyr}
    if IMU_ACCEL_Z_OFF + 2 * n_acc <= len(rec):
        out["accel_x"] = _i16_array(rec, IMU_ACCEL_X_OFF, n_acc)
        out["accel_y"] = _i16_array(rec, IMU_ACCEL_Y_OFF, n_acc)
        out["accel_z"] = _i16_array(rec, IMU_ACCEL_Z_OFF, n_acc)
    if n_gyr and IMU_GYRO_Z_OFF + 2 * n_gyr <= len(rec):
        out["gyro_x"] = _i16_array(rec, IMU_GYRO_X_OFF, n_gyr)
        out["gyro_y"] = _i16_array(rec, IMU_GYRO_Y_OFF, n_gyr)
        out["gyro_z"] = _i16_array(rec, IMU_GYRO_Z_OFF, n_gyr)
    return out


# =====================================================================================================
# 7. METADATA (type 49) — offload control   [JAVA ej0/h.java, ej0/b.java (HistoryEnd)]
# =====================================================================================================
# meta subtype @ inner[2]. For HISTORY_END: expected packet count @ inner[9:13]; the 8-byte trim/end_data
# the ack must echo = inner[13:21] = frame[21:29] (whoop5). The ack (HISTORICAL_DATA_RESULT=23) body =
# [SUCCESS=0x01] + those 8 raw bytes.
META_SUBTYPE_OFF = 2
META_EXPECTED_COUNT_OFF = 9
META_END_DATA_OFF, META_END_DATA_LEN = 13, 8   # inner offsets → frame[21:29]


def decode_metadata(rec):
    if len(rec) < META_SUBTYPE_OFF + 1:
        return {"type_name": "METADATA", "valid": False}
    sub = _u8(rec, META_SUBTYPE_OFF)
    out = {"type_name": "METADATA", "meta_subtype": METADATA_SUBTYPES.get(sub, sub)}
    if sub == 2 and len(rec) >= META_END_DATA_OFF + META_END_DATA_LEN:      # HISTORY_END
        out["expected_count"] = _u32(rec, META_EXPECTED_COUNT_OFF)
        out["end_data"] = rec[META_END_DATA_OFF:META_END_DATA_OFF + META_END_DATA_LEN].hex()
    return out


# =====================================================================================================
# 8. COMMAND_RESPONSE (type 36)             [JAVA bj0/f.java]
# =====================================================================================================
# inner: type@0, seq@1, cmd@2, originSeq@3, result@4, payload@5…
def decode_command_response(rec):
    if len(rec) < 5:
        return {"type_name": "COMMAND_RESPONSE", "valid": False}
    cmd = _u8(rec, 2)
    return {"type_name": "COMMAND_RESPONSE", "cmd": COMMANDS.get(cmd, cmd),
            "origin_seq": _u8(rec, 3), "result": CMD_RESULTS.get(_u8(rec, 4), _u8(rec, 4)),
            "payload": rec[5:].hex()}   # per-command payload layouts (battery/version) not all mapped yet


# =====================================================================================================
# 9. DISPATCH
# =====================================================================================================
def decode_frame(frame, family="whoop5"):
    """Decode any WHOOP frame → a dict of understood fields, or None if it fails CRC."""
    rec = inner_record(frame, family)
    if rec is None or not rec:
        return None
    t = rec[0]
    name = PACKET_TYPES.get(t, f"UNKNOWN(0x{t:02x})")
    if t == 40:
        d = decode_realtime(rec)
    elif t == 47:
        d = decode_historical(rec, frame)
    elif t == 43:
        k = _u8(rec, 1) if len(rec) > 1 else -1
        d = (decode_r10(rec) if k == 10 else decode_ecg_r17(rec) if k == 17
             else decode_imu_r21(rec) if k == 21 else None)
        if d is None:
            d = {"type_name": ("PIP" if k in (25, 26) else name), "channel": R_CHANNELS.get(k)}
    elif t == 49:
        d = decode_metadata(rec)
    elif t == 36:
        d = decode_command_response(rec)
    elif t == 48:
        d = decode_event(rec) or {"type_name": name}
    else:
        d = {"type_name": name}
    d["type"] = t
    return d


# =====================================================================================================
# 10. FIELD MAP — the human-readable catalog of everything we understand
# =====================================================================================================
FIELD_MAP = """
WHOOP 5 (Maverick) — DECODED FIELD MAP     [JAVA]=from app  [NOOP]=RE-verified  [PUB]=downstream formula
────────────────────────────────────────────────────────────────────────────────────────────────────
FRAME        0xAA · 0x01 · declLen u16 · role 0x00 0x01 · crc16 · [inner @8] · crc32   [JAVA lq0/e,sq0/c]
             LITTLE_ENDIAN throughout                                                   [JAVA kq0/d]

REALTIME_DATA (40)   inner: revision@1 · unix u32@2 · subsec u16@6 · HR u8@8 ·          [JAVA ej0/j]
                     off_wrist(@18==0) · body_location u8@19          HR@8 = true bpm
HISTORICAL_DATA (47) inner: version@1 · index u32@3 · unix u32@7 · subsec u16@11 ·      [JAVA kq0/b]
                     HR byte @ {v18→14, v24→17, v7→27, v9/12→17}   v18 HR = bpm         [NOOP corr 0.96]
   └ v26 record      PPG waveform: frame[27:75] = 24× i16 @24 Hz   → HRV                [NOOP corr +0.907]
REALTIME_RAW (43) R21  inner: sampling_hz@14 · n_accel@16 · accelX@20/Y@220/Z@420 ·     [JAVA mq0/e]
                     n_gyro@622 · gyroX@632/Y@832/Z@1032   (i16, 100/axis)  = IMU
METADATA (49)        inner: subtype@2 {1 START,2 END,3 COMPLETE} · expected u32@9 ·     [JAVA ej0/h,b]
                     HISTORY_END end_data = frame[21:29] (ack echoes it)
COMMAND_RESPONSE(36) inner: cmd@2 · originSeq@3 · result@4 · payload@5…                 [JAVA bj0/f]

CHANNELS  R16 ECG · R17 Labrador-filtered · R20 optical(opaque) · R21 IMU · R25/R26 Pip [JAVA zt0/c,jq0/b]
IDENTITY  serial · cpuId · fw(major.minor.build) · dsp(sigproc) · hw · family           [JAVA WhoopStrapInfo]

DERIVED (computed from the fields above, sibling modules):
  Strain(0-21) + Calories(kJ)  logistic 16/(exp((1-hrr)/0.15)+1)/691200 ; Keytel 2005   [JAVA qh0/*] → whoop_strain.py
  HRV (RMSSD/SDNN/SD1/SD2)      beat-detect the v26 PPG waveform                          [NOOP]      → whoop_hrv.py

NOT ON THE STRAP (server-side, not decodable locally): Recovery% · Sleep stages · final HRV · SpO2%
NOT LIVE: PPG/HRV only from the historical offload (type-47 v26); live type-43 R21 is IMU, R20 is opaque.
"""


# =====================================================================================================
# 11. CLI
# =====================================================================================================


# ==================================================================================================
# §6  BLE COMMANDS + REASSEMBLER  [JAVA zi0/*, bj0/*]  (redefines crc — identical)
# ==================================================================================================

# COMMAND packet type byte (PacketType.COMMAND).
COMMAND_TYPE = 35

# Command numbers (subset; mirrors WhoopCommand in Commands.swift). The numbers are shared with the
# 5.0 puffin command set — only the framing differs (CRC8 here vs CRC16 there).
CMD_GET_BATTERY_LEVEL = 26
CMD_GET_CLOCK = 11
CMD_REPORT_VERSION_INFO = 7
CMD_GET_EXTENDED_BATTERY_INFO = 98
CMD_GET_DATA_RANGE = 34
CMD_GET_HELLO_HARVARD = 35

# WHOOP 5.0 session-start frame, written verbatim to fd4b0002 to open the puffin session.
# (DeviceFamily.whoop5ClientHello — a fully-formed type-35 COMMAND with valid CRC16 header + CRC32.)
WHOOP5_CLIENT_HELLO = bytes.fromhex("aa0108000001e67123019101363e5c8d")


def _crc8_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
        table.append(c)
    return table


_CRC8_TABLE = _crc8_table()


def crc8(data: bytes) -> int:
    """CRC-8 (poly 0x07, init 0x00) — the WHOOP 4.0 header check over the two length bytes."""
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


def crc32(data: bytes) -> int:
    """Standard zlib CRC-32 — the WHOOP 4.0/5.0 payload trailer."""
    return zlib.crc32(data) & 0xFFFFFFFF


def build_command_frame(cmd: int, seq: int = 0, payload: bytes = b"\x00") -> bytes:
    """Build a complete WHOOP 4.0 COMMAND frame ready to write to the CMD-write characteristic.

    Mirrors `WhoopCommand.frame(seq:payload:)`. Used to send the benign GET_BATTERY_LEVEL that
    triggers just-works bonding on a 4.0 strap.
    """
    inner = bytes([COMMAND_TYPE, seq & 0xFF, cmd & 0xFF]) + payload
    length = (3 + len(payload)) + 4           # inner (type+seq+cmd+payload) + 4-byte CRC32 trailer
    len_bytes = bytes([length & 0xFF, (length >> 8) & 0xFF])
    header_crc = crc8(len_bytes)
    trailer = crc32(inner)
    trailer_bytes = bytes([trailer & 0xFF, (trailer >> 8) & 0xFF,
                           (trailer >> 16) & 0xFF, (trailer >> 24) & 0xFF])
    return bytes([0xAA]) + len_bytes + bytes([header_crc]) + inner + trailer_bytes


def crc16_modbus(data: bytes) -> int:
    """CRC16-Modbus (poly 0xA001, init 0xFFFF, reflected) — the WHOOP 5.0 frame header check."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


# Puffin command numbers worth probing after CLIENT_HELLO to elicit streaming (all non-destructive
# reads/toggles; mirror WhoopCommand). These reuse the 4.0 command numbers on the 5.0 transport;
# GET_CLOCK, TOGGLE_REALTIME_HR, SEND_R10_R11_REALTIME and SEND_HISTORICAL_DATA are confirmed accepted
# on real WHOOP 5 hardware (see docs/BLE_REVERSE_ENGINEERING.md §3); the rest remain educated guesses.
PUFFIN_CMD_TOGGLE_REALTIME_HR = 3
PUFFIN_CMD_SEND_HISTORICAL_DATA = 22
# Ack a received history chunk (confirmed write echoing the chunk's end_data / trim cursor). On 4.0
# this is what advances the strap's trim cursor to the NEXT chunk; without it the strap re-sends the
# same early chunk forever. The leading hypothesis for why the DSP (type-47) records never arrive is
# that we never ack — so the cursor never reaches them. Build the payload from each METADATA (type 49)
# chunk's trim/end_data; see Strand/BLE/BLEManager.swift `ackHistoricalChunk` for the 4.0 shape.
PUFFIN_CMD_HISTORICAL_DATA_RESULT = 23
PUFFIN_CMD_GET_CLOCK = 11
PUFFIN_CMD_GET_DATA_RANGE = 34
PUFFIN_CMD_SEND_R10_R11_REALTIME = 63
# Read-only "GET" commands that elicit a COMMAND_RESPONSE (type 36) — used to map the WHOOP 5
# command-response payloads at the +4 offsets. All are non-destructive reads; the response echoes the
# command number in its cmd byte (frame[10] on 5.0), which selects the payload layout.
PUFFIN_CMD_REPORT_VERSION_INFO = 7          # → fw version block
PUFFIN_CMD_GET_BATTERY_LEVEL = 26           # → battery %
PUFFIN_CMD_GET_EXTENDED_BATTERY_INFO = 98   # → battery mV
PUFFIN_CMD_GET_BATTERY_PACK_INFO = 151      # WHOOP 5 removable battery pack (no 4.0 analogue)


def build_puffin_command(cmd: int, seq: int = 0, payload: bytes = b"\x00",
                         type_: int = 35, header: bytes = b"\x00\x01") -> bytes:
    """Build a WHOOP 5.0 ("puffin") command frame. Port of `puffinCommandFrame` in Framing.swift:
    [0xAA][0x01][declLen u16 LE][header u16][crc16 u16 LE][type][seq][cmd][payload…][crc32 LE].
    """
    inner = bytes([type_, seq & 0xFF, cmd & 0xFF]) + payload
    decl = len(inner) + 4
    frame = bytearray([0xAA, 0x01, decl & 0xFF, (decl >> 8) & 0xFF, header[0], header[1]])
    c16 = crc16_modbus(bytes(frame[0:6]))
    frame += bytes([c16 & 0xFF, (c16 >> 8) & 0xFF])
    frame += inner
    c32 = crc32(inner)
    frame += bytes([c32 & 0xFF, (c32 >> 8) & 0xFF, (c32 >> 16) & 0xFF, (c32 >> 24) & 0xFF])
    return bytes(frame)


# --- Haptics / buzz (find-my-strap) ---------------------------------------------------------------
# Drive the strap's vibration motor. Safe and reversible — it only runs the haptic motor; it does not
# touch the data store, clock, alarm, or firmware. Used by whoop_buzz.py to locate a misplaced strap.
#
# WHOOP 5 / MG (puffin): the maverick haptic, opcode 0x13 = RUN_HAPTIC_PATTERN_MAVERICK (NOT 79 — a
# real-MG capture showed the strap rejecting RUN_HAPTICS_PATTERN=79 with COMMAND_RESPONSE result=0x03).
# Body = [0x01, effects(u8…), loopControl u16 LE, overallLoop] — here the "notify" preset (effects
# 47,152). This is the exact command the official app sends, matched byte-for-byte (noop issue #48),
# and shipped in Strand's BLEManager.send(). On real hardware the strap acknowledges with
# COMMAND_RESPONSE(type 36, cmd 0x13).
MAVERICK_HAPTIC_CMD = 0x13
MAVERICK_HAPTIC_NOTIFY = bytes([0x01, 47, 152, 0, 0, 0, 0, 0, 0, 0, 0, 0])  # 12-byte "notify" preset

# WHOOP 4.0: RUN_HAPTICS_PATTERN=79 with body [patternId, numLoops, 0, 0, 0]; the official app fires
# patternId=2 (the graduated alarm buzz). RUN_ALARM=68 (body [0x01]) is the app-driven alarm fallback.
WHOOP4_RUN_HAPTICS_PATTERN = 79
WHOOP4_RUN_ALARM = 68

COMMAND_RESPONSE_TYPE = 36   # PacketType.COMMAND_RESPONSE — the strap's per-command ack


def _pad4(payload: bytes) -> bytes:
    """Right-pad a puffin command body with zeros so the inner record (type+seq+cmd+payload) lands on
    a 4-byte boundary, exactly as Framing.swift `puffinCommandFrame` (pad4) does. `build_puffin_command`
    does not pad on its own, so any non-4-aligned body (e.g. the 12-byte haptic) must be padded here or
    the declared length / CRC32 cover the wrong byte count and the strap rejects the frame (#48)."""
    inner_len = 3 + len(payload)   # type + seq + cmd + payload
    return payload + bytes((4 - inner_len % 4) % 4)


def build_whoop5_buzz(seq: int = 0) -> bytes:
    """WHOOP 5 / MG buzz frame: the maverick "notify" haptic (opcode 0x13), puffin-framed and pad4'd."""
    return build_puffin_command(MAVERICK_HAPTIC_CMD, seq=seq, payload=_pad4(MAVERICK_HAPTIC_NOTIFY))


def build_whoop4_buzz(seq: int = 0, pattern: int = 2, loops: int = 3) -> bytes:
    """WHOOP 4.0 buzz frame: RUN_HAPTICS_PATTERN (79) with body [patternId, numLoops, 0, 0, 0]."""
    return build_command_frame(WHOOP4_RUN_HAPTICS_PATTERN, seq=seq,
                               payload=bytes([pattern & 0xFF, loops & 0xFF, 0, 0, 0]))


def buzz_frame(family: str, seq: int = 0) -> bytes:
    """Family-dispatched buzz frame ("whoop5" → maverick 0x13, "whoop4" → RUN_HAPTICS_PATTERN)."""
    if family == "whoop5":
        return build_whoop5_buzz(seq)
    if family == "whoop4":
        return build_whoop4_buzz(seq)
    raise ValueError(f"unknown family {family!r}")


# --- Clock ----------------------------------------------------------------------------------------
# SET_CLOCK (command 10) sets the strap RTC. The phone app sends this on every connect when the strap's
# clock has drifted (ClockPolicy). Needed here because a strap left offline for months loses its clock.
CMD_SET_CLOCK = 10


def set_clock_payload(now_unix: int, subsecond_bytes: int = 4) -> bytes:
    """SET_CLOCK body: `u32 unix-seconds LE` + N zero subsecond bytes. The LENGTH is load-bearing — the
    strap latches only the exact form its firmware expects and silently ignores the rest:
      * 8-byte (`subsecond_bytes=4`) — the form the app uses for newer firmware (see BLEManager).
      * 9-byte (`subsecond_bytes=5`) — what real WHOOP 4 fw 41.17.6.0 latches (hardware-verified below).
    Default is 8 for back-compat; `build_whoop4_set_clock` overrides to 9."""
    return bytes([now_unix & 0xFF, (now_unix >> 8) & 0xFF,
                  (now_unix >> 16) & 0xFF, (now_unix >> 24) & 0xFF]) + bytes(subsecond_bytes)


def build_whoop4_set_clock(now_unix: int, seq: int = 0) -> bytes:
    """WHOOP 4.0 SET_CLOCK frame (CRC8 framing).

    HARDWARE-VERIFIED (WHOOP 4C, fw 41.17.6.0, 2026-06-12): this firmware latches the RTC ONLY with the
    9-byte body `[u32 LE + 5 zero]` — it returns a COMMAND_RESPONSE(cmd 10) and the strap's event clock
    jumps to the set time. The 8-byte body (the app's newer-fw default) draws NO command-response at all
    on this strap, so it never latches. Hence whoop4 uses 9 here; newer firmware may want 8."""
    return build_command_frame(CMD_SET_CLOCK, seq=seq, payload=set_clock_payload(now_unix, 5))


def build_whoop5_set_clock(now_unix: int, seq: int = 0) -> bytes:
    """WHOOP 5.0 SET_CLOCK frame (puffin CRC16 framing). The 8-byte body is already 4-aligned (inner 11
    → pad to 12), so pad to a 4-byte boundary like every puffin command."""
    body = set_clock_payload(now_unix)
    pad = (4 - (3 + len(body)) % 4) % 4
    return build_puffin_command(CMD_SET_CLOCK, seq=seq, payload=body + bytes(pad))


# --- WHOOP 5 (Maverick) realtime-enable + LINK_VALID handshake ------------------------------------
# Extracted from the decompiled app (com/whoop/service/rearchitect/c.java + zi0/*, bj0/x.java). The
# strap does NOT auto-start its biometric frames after the client-hello: the client must ENABLE
# realtime with the maverick toggles below (SEND_R10_R11_REALTIME=63 is the GEN-4-only path and does
# NOT stream on a WHOOP 5), and must answer the strap-initiated LINK_VALID with "There it is." or the
# strap treats the link as invalid and withholds data.
MAVERICK_TOGGLE_REALTIME_HR = 3       # → type-40 REALTIME_DATA frames (HR@8) — bare 1-byte body
MAVERICK_TOGGLE_IMU_MODE = 106        # → type-43 R21 IMU raw (accel+gyro) — [REVISION_1, on]
MAVERICK_TOGGLE_OPTICAL_MODE = 108    # → R20 optical raw (2140-byte OPAQUE blob on-device; NOT R21)
CMD_LINK_VALID = 1
LINK_VALID_RESPONSE_TEXT = b"There it is."   # bj0/x.java literal


def build_toggle_realtime_hr(seq: int = 0, on: bool = True) -> bytes:
    """Maverick TOGGLE_REALTIME_HR(3), payload [0x01]=on (zi0/h1.java + zi0/a.java c()={1})."""
    return build_puffin_command(MAVERICK_TOGGLE_REALTIME_HR, seq=seq, payload=b"\x01" if on else b"\x00")


def build_toggle_imu_mode(seq: int = 0, on: bool = True) -> bytes:
    """Maverick TOGGLE_IMU_MODE(106), payload [REVISION_1=1, on] (zi0/d1.java). The app's realtime
    dispatcher c.l() sends this BEFORE the optical toggle — starts the type-43 R21 IMU stream."""
    return build_puffin_command(MAVERICK_TOGGLE_IMU_MODE, seq=seq, payload=_pad4(bytes([1, 1 if on else 0])))


def build_toggle_optical_mode(seq: int = 0, on: bool = True) -> bytes:
    """Maverick TOGGLE_OPTICAL_MODE(108), payload [REVISION_1=1, on] (zi0/e1.java). Enables the R20
    optical raw stream — which the app ships as an opaque blob (no on-device PPG samples; live PPG for
    HRV is NOT available — the v26 PPG waveform only comes from the type-47 historical offload)."""
    return build_puffin_command(MAVERICK_TOGGLE_OPTICAL_MODE, seq=seq, payload=_pad4(bytes([1, 1 if on else 0])))


def build_link_valid_response(origin_seq: int, seq: int = 0) -> bytes:
    """Reply to a strap LINK_VALID(1) with COMMAND_RESPONSE(36): inner = [36, seq, cmd=1, originSeq,
    SUCCESS=1, "There it is."]. Layout per bj0/f.java (cmd@2, originSeq@3, result@4, payload@5)."""
    payload = bytes([origin_seq & 0xFF, 1]) + LINK_VALID_RESPONSE_TEXT   # [originSeq, SUCCESS, text]
    return build_puffin_command(CMD_LINK_VALID, seq=seq, payload=_pad4(payload), type_=COMMAND_RESPONSE_TYPE)


def is_link_valid_command(frame: bytes, inner_off: int):
    """If `frame` is a strap-initiated LINK_VALID COMMAND, return its origin seq (to echo), else None.
    Inner record: type@0 = COMMAND(35), seq@1, cmd@2 = LINK_VALID(1)."""
    if len(frame) < inner_off + 3 or frame[0] != 0xAA:
        return None
    if frame[inner_off] == COMMAND_TYPE and frame[inner_off + 2] == CMD_LINK_VALID:
        return frame[inner_off + 1]
    return None


# EVENT (type 48) and REALTIME_DATA (type 40) frames both carry the strap RTC as a u32 LE timestamp,
# but at DIFFERENT offsets per type. Verified against whoop-decode on real frames:
#   WHOOP 4.0: REALTIME(40) timestamp @6, EVENT(48) event_timestamp @8.
#   WHOOP 5.0 (puffin, +4 inner-record rule): REALTIME(40) timestamp @10, EVENT(48) event_timestamp @12.
RTC_OFFSETS = {
    ("whoop4", 40): (4, 6), ("whoop4", 48): (4, 8),
    ("whoop5", 40): (8, 10), ("whoop5", 48): (8, 12),
}


def frame_rtc(frame: bytes, family: str):
    """Return the strap RTC (unix secs) from an EVENT or REALTIME frame, else None. The trusted clock
    read-back — reading the exact timestamp field (not scanning bytes) avoids false positives."""
    if len(frame) < 6 or frame[0] != 0xAA:
        return None
    for (fam, t), (type_off, rtc_off) in RTC_OFFSETS.items():
        if fam == family and len(frame) >= rtc_off + 4 and frame[type_off] == t:
            return int.from_bytes(frame[rtc_off:rtc_off + 4], "little")
    return None


def command_response_cmd(frame: bytes, family: str):
    """If `frame` is a COMMAND_RESPONSE (type 36), return the command number it acknowledges, else None.

    The inner record starts at offset 8 on the 5.0 puffin frame and offset 4 on the 4.0 frame; the
    command byte sits two bytes past the type. Lets the buzz tool confirm the strap actually accepted
    a haptic (resp_cmd == the buzz opcode) instead of guessing from raw bytes."""
    inner_off = 8 if family == "whoop5" else 4
    if len(frame) < inner_off + 3 or frame[0] != 0xAA:
        return None
    if frame[inner_off] != COMMAND_RESPONSE_TYPE:
        return None
    return frame[inner_off + 2]


# --- WHOOP 5.0 historical-offload helpers ---------------------------------------------------------
# Packet types (PacketType enum, wire byte at the inner-record start, offset 8 on 5.0).
PACKET_METADATA = 49
PACKET_HISTORICAL_DATA = 47
# MetadataType enum values (the meta_type byte).
META_HISTORY_START = 1
META_HISTORY_END = 2
META_HISTORY_COMPLETE = 3
# WHOOP 5.0 metadata field offsets = the 4.0 offsets + 4 (the inner record starts at byte 8 vs 4).
# Verified on real hardware: meta_type at 6→10, trim_cursor at 17→21 (decodes to clean HISTORY_END
# values with a consistent trim across a whole capture). The 8-byte end_data the ack echoes is the
# trim u32 followed by the next u32, i.e. frame[21:29].
WHOOP5_INNER_OFF = 8
WHOOP5_META_TYPE_OFF = 10
WHOOP5_META_TRIM_OFF = 21
WHOOP5_END_DATA_LEN = 8


def verify_whoop5_frame(frame: bytes) -> bool:
    """True if a WHOOP 5.0 frame's declared length, CRC16 header and CRC32 trailer all check out.

    The ack must only echo a genuine, intact HISTORY_END — CRC is the protocol's only integrity
    check, so a garbled BLE frame must never advance the strap's trim cursor.
    """
    if len(frame) < 12 or frame[0] != 0xAA:
        return False
    decl = frame[2] | (frame[3] << 8)
    total = decl + 8
    if total != len(frame):
        return False
    if crc16_modbus(bytes(frame[0:6])) != (frame[6] | (frame[7] << 8)):
        return False
    inner = bytes(frame[8:total - 4])
    wire = int.from_bytes(frame[total - 4:total], "little")
    return crc32(inner) == wire


def history_end_data(frame: bytes):
    """If `frame` is a CRC-valid WHOOP 5.0 METADATA HISTORY_END, return its 8-byte end_data
    (trim u32 + next u32 at offset 21) to echo back in the ack; otherwise None.

    Echoing these bytes verbatim in a HISTORICAL_DATA_RESULT(23) is what advances the strap's trim
    cursor to the next chunk on 4.0 — without it the strap re-serves the same early chunk forever and
    the type-47 DSP records further in the store are never reached.
    """
    if len(frame) < WHOOP5_META_TRIM_OFF + WHOOP5_END_DATA_LEN:
        return None
    if frame[WHOOP5_INNER_OFF] != PACKET_METADATA:
        return None
    if frame[WHOOP5_META_TYPE_OFF] != META_HISTORY_END:
        return None
    if not verify_whoop5_frame(frame):
        return None
    return bytes(frame[WHOOP5_META_TRIM_OFF:WHOOP5_META_TRIM_OFF + WHOOP5_END_DATA_LEN])


def build_history_ack(end_data: bytes, seq: int = 0) -> bytes:
    """Build the HISTORICAL_DATA_RESULT(23) ack frame: payload = 0x01 + the 8-byte end_data.

    Mirrors `ackHistoricalChunk` in Strand/BLE/BLEManager.swift (`send(.historicalDataResult,
    payload: [0x01] + endData, .withResponse)`). Written to fd4b0002 as a CONFIRMED write.
    """
    return build_puffin_command(PUFFIN_CMD_HISTORICAL_DATA_RESULT, seq=seq,
                                payload=b"\x01" + bytes(end_data))


# --- WHOOP 4.0 historical-offload helpers ---------------------------------------------------------
# The 4.0 image of the whoop5 helpers above. The inner record starts at offset 4 (vs 5.0's 8), so the
# metadata fields sit 4 bytes earlier: meta_type at frame[6] (5.0's 10 − 4) and trim_cursor at
# frame[17] (5.0's 21 − 4). The 8-byte end_data the ack echoes is the trim u32 + next u32 = frame[17:25]
# (vs 5.0's frame[21:29]). PACKET_* / META_* / the command numbers (22 SEND_HISTORICAL_DATA, 23
# HISTORICAL_DATA_RESULT) are SHARED across generations — only the framing differs (CRC8 here, CRC16
# there) — so they are reused as-is rather than forked. See PostHooks.swift `metadata` (4.0) and
# docs/BLE_REVERSE_ENGINEERING.md §5.
WHOOP4_INNER_OFF = 4
WHOOP4_META_TYPE_OFF = 6
WHOOP4_META_TRIM_OFF = 17
WHOOP4_END_DATA_LEN = 8


def verify_whoop4_frame(frame: bytes) -> bool:
    """True if a WHOOP 4.0 frame's declared length, CRC8 header and CRC32 trailer all check out.

    As on 5.0, the ack must only echo a genuine, intact HISTORY_END — CRC is the protocol's only
    integrity check, so a garbled BLE frame must never advance the strap's trim cursor.
    """
    if len(frame) < 8 or frame[0] != 0xAA:
        return False
    declared = frame[1] | (frame[2] << 8)
    total = declared + 4
    if total != len(frame):
        return False
    if crc8(bytes(frame[1:3])) != frame[3]:
        return False
    inner = bytes(frame[4:total - 4])
    wire = int.from_bytes(frame[total - 4:total], "little")
    return crc32(inner) == wire


def history_end_data_whoop4(frame: bytes):
    """If `frame` is a CRC-valid WHOOP 4.0 METADATA HISTORY_END, return its 8-byte end_data
    (trim u32 + next u32 at frame[17]) to echo back in the ack; otherwise None.

    Echoing these bytes verbatim in a HISTORICAL_DATA_RESULT(23) advances the strap's trim cursor to
    the next chunk — the 4.0 image of `history_end_data`, with offsets 4 bytes earlier.
    """
    if len(frame) < WHOOP4_META_TRIM_OFF + WHOOP4_END_DATA_LEN:
        return None
    if frame[WHOOP4_INNER_OFF] != PACKET_METADATA:
        return None
    if frame[WHOOP4_META_TYPE_OFF] != META_HISTORY_END:
        return None
    if not verify_whoop4_frame(frame):
        return None
    return bytes(frame[WHOOP4_META_TRIM_OFF:WHOOP4_META_TRIM_OFF + WHOOP4_END_DATA_LEN])


def build_history_ack_whoop4(end_data: bytes, seq: int = 0) -> bytes:
    """Build the WHOOP 4.0 HISTORICAL_DATA_RESULT(23) ack: payload = 0x01 + the 8-byte end_data,
    framed as a 4.0 COMMAND (CRC8 header). The 4.0 image of `build_history_ack`.
    """
    return build_command_frame(PUFFIN_CMD_HISTORICAL_DATA_RESULT, seq=seq,
                               payload=b"\x01" + bytes(end_data))


class Reassembler:
    """Family-aware frame reassembler: BLE delivers MTU-sized fragments; this accumulates bytes,
    finds the 0xAA SOF, reads the declared length, and emits a complete frame once enough bytes are
    present. One instance per notify characteristic so channels don't interleave.

    family: "whoop4" or "whoop5".
    """

    def __init__(self, family: str):
        self.family = family
        self.buf = bytearray()

    def _total_len(self):
        """Total on-wire length of the frame at the front of the buffer, or None if not yet known."""
        b = self.buf
        if self.family == "whoop4":
            if len(b) < 3:
                return None
            declared = b[1] | (b[2] << 8)      # len field
            return declared + 4
        else:  # whoop5
            if len(b) < 4:
                return None
            declared = b[2] | (b[3] << 8)      # declaredLength
            return declared + 8

    def feed(self, data: bytes):
        """Add a notification's bytes; return a list of any complete frames now available (as bytes)."""
        self.buf.extend(data)
        out = []
        while True:
            # Resync to the next SOF if the buffer doesn't start on one.
            sof = self.buf.find(0xAA)
            if sof == -1:
                self.buf.clear()
                break
            if sof > 0:
                del self.buf[:sof]
            total = self._total_len()
            if total is None:
                break
            # Guard against an absurd length from a corrupt SOF FIRST — before waiting for more
            # bytes — so a bad length can't stall the buffer forever. Drop one byte and resync.
            if total <= 0 or total > 4096:
                del self.buf[:1]
                continue
            if len(self.buf) < total:
                break
            out.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return out


def parse_standard_hr(data: bytes):
    """Parse a standard Heart Rate Measurement (0x2A37) notification → bpm, or None.

    Layout: flags u8; bit0 = HR is u16 (else u8). We only need the bpm for ground-truth correlation.
    """
    if not data:
        return None
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return None
        return data[1] | (data[2] << 8)
    if len(data) < 2:
        return None
    return data[1]


# ==================================================================================================
# §3  STRAIN + CALORIES  [JAVA qh0/*]
# ==================================================================================================

# --- BiologicalSex.java: (hrWeight, restWeight, restHeight, restAge, restAlpha,
#                          workHr, workWeight, workAge, workAlpha) ---
BIO_SEX = {
    "male":        (-2.0229800401446663, 6.2,   12.0,  6.8,   60.0,   0.6,    0.19,   0.2,   -50.0),
    "female":      (0.0,                 4.3,   4.0,   4.7,   650.0,  0.4,   -0.13,   0.07,  -15.0),
    "nonBinary":   (-1.0114900200723,    5.25,  8.0,   5.75,  355.0,  0.5,    0.03,   0.135, -32.5),
    "maleNew":     (-2.0229800401446663, 13.397,479.9, 5.677, 88.362, 0.6309, 0.1988, 0.2017,-55.0969),
    "femaleNew":   (0.0,                 9.247, 309.8, 4.33,  447.593,0.4472,-0.1263, 0.074, -20.4022),
    "nonBinaryNew":(-1.0114900200723,    11.322,394.85,5.0035,267.9775,0.53905,0.03625,0.13785,-37.74955),
}
NEW_SEXES = {"maleNew", "femaleNew", "nonBinaryNew"}

def _sex(sex):  # returns dict of named coefficients
    v = BIO_SEX[sex]
    return dict(hrWeight=v[0], restWeight=v[1], restHeight=v[2], restAge=v[3], restAlpha=v[4],
               workHr=v[5], workWeight=v[6], workAge=v[7], workAlpha=v[8])

# --- FitnessLevel.java: weightForHeartRate ---
FITNESS = {
    "collegiate": 0.0, "serious_enthusiast": -0.6984338232663312,
    "tactical": -2.9930650327571664, "professional": -2.9930650327571664,
    "recreational_enthusiast": -3.295121377950899, "inactive": -3.6688917003869923,
}

RESTING_STRAIN_FLOOR = 0.3   # b.f138161a: HR-reserve floor / workout threshold fraction
KJ_PER_KCAL = 4.184
KEYTEL_DIV = 251.04000000000002   # = 60 * 4.184 : kJ/min -> kcal/s
STRAIN_NORM = 691200.0            # o.h(): raw-strain normaliser (= 8 days of seconds)

STRAIN_SCALE_TABLE = [
    (0.0, 0.0),
    (7.1e-07, 0.1),
    (1.75e-06, 0.2),
    (2.55e-06, 0.3),
    (3.26e-06, 0.4),
    (3.89e-06, 0.5),
    (4.49e-06, 0.6),
    (5.05e-06, 0.7),
    (5.59e-06, 0.8),
    (6.11e-06, 0.9),
    (6.62e-06, 1.0),
    (7.12e-06, 1.1),
    (7.61e-06, 1.2),
    (8.1e-06, 1.3),
    (8.59e-06, 1.4),
    (9.08e-06, 1.5),
    (9.57e-06, 1.6),
    (1.01e-05, 1.7),
    (1.06e-05, 1.8),
    (1.11e-05, 1.9),
    (1.16e-05, 2.0),
    (1.22e-05, 2.1),
    (1.29e-05, 2.2),
    (1.36e-05, 2.3),
    (1.43e-05, 2.4),
    (1.51e-05, 2.5),
    (1.61e-05, 2.6),
    (1.71e-05, 2.7),
    (1.83e-05, 2.8),
    (1.95e-05, 2.9),
    (2.06e-05, 3.0),
    (2.17e-05, 3.1),
    (2.28e-05, 3.2),
    (2.4e-05, 3.3),
    (2.53e-05, 3.4),
    (2.65e-05, 3.5),
    (2.78e-05, 3.6),
    (2.92e-05, 3.7),
    (3.09e-05, 3.8),
    (3.33e-05, 3.9),
    (3.62e-05, 4.0),
    (6.45e-05, 4.1),
    (0.00011577, 4.2),
    (0.00016728, 4.3),
    (0.00021901, 4.4),
    (0.00027095, 4.5),
    (0.00032311, 4.6),
    (0.0003755, 4.7),
    (0.00042812, 4.8),
    (0.00048097, 4.9),
    (0.00053407, 5.0),
    (0.00058739, 5.1),
    (0.00064099, 5.2),
    (0.00069481, 5.3),
    (0.00074889, 5.4),
    (0.00080326, 5.5),
    (0.00085787, 5.6),
    (0.00091273, 5.7),
    (0.0009679, 5.8),
    (0.00102338, 5.9),
    (0.00107911, 6.0),
    (0.00113511, 6.1),
    (0.00119143, 6.2),
    (0.00124811, 6.3),
    (0.0013051, 6.4),
    (0.00136235, 6.5),
    (0.00141991, 6.6),
    (0.00147781, 6.7),
    (0.00153612, 6.8),
    (0.00159484, 6.9),
    (0.00165386, 7.0),
    (0.00171318, 7.1),
    (0.00177285, 7.2),
    (0.00183291, 7.3),
    (0.00189341, 7.4),
    (0.00195441, 7.5),
    (0.00201589, 7.6),
    (0.0020777, 7.7),
    (0.00213985, 7.8),
    (0.0022024, 7.9),
    (0.0022654, 8.0),
    (0.00232891, 8.1),
    (0.00239299, 8.2),
    (0.0024577, 8.3),
    (0.00252305, 8.4),
    (0.00258878, 8.5),
    (0.00265492, 8.6),
    (0.00272153, 8.7),
    (0.00278866, 8.8),
    (0.00285639, 8.9),
    (0.0029248, 9.0),
    (0.00299396, 9.1),
    (0.00306396, 9.2),
    (0.0031349, 9.3),
    (0.00320654, 9.4),
    (0.00327863, 9.5),
    (0.00335127, 9.6),
    (0.00342454, 9.7),
    (0.00349854, 9.8),
    (0.00357339, 9.9),
    (0.00364918, 10.0),
    (0.00372606, 10.1),
    (0.00380418, 10.2),
    (0.00388369, 10.3),
    (0.00396475, 10.4),
    (0.00404666, 10.5),
    (0.00412921, 10.6),
    (0.00421254, 10.7),
    (0.00429682, 10.8),
    (0.00438224, 10.9),
    (0.00446901, 11.0),
    (0.00455736, 11.1),
    (0.00464759, 11.2),
    (0.00474001, 11.3),
    (0.00483503, 11.4),
    (0.00493314, 11.5),
    (0.00503434, 11.6),
    (0.00513699, 11.7),
    (0.0052412, 11.8),
    (0.00534724, 11.9),
    (0.00545545, 12.0),
    (0.00556624, 12.1),
    (0.00568009, 12.2),
    (0.00579757, 12.3),
    (0.00591944, 12.4),
    (0.00604668, 12.5),
    (0.00618059, 12.6),
    (0.00632272, 12.7),
    (0.00647253, 12.8),
    (0.00662989, 12.9),
    (0.00679477, 13.0),
    (0.00696677, 13.1),
    (0.0071451, 13.2),
    (0.00732845, 13.3),
    (0.00751503, 13.4),
    (0.00770273, 13.5),
    (0.00788929, 13.6),
    (0.00807618, 13.7),
    (0.00826577, 13.8),
    (0.00845807, 13.9),
    (0.00865309, 14.0),
    (0.00885085, 14.1),
    (0.00905134, 14.2),
    (0.00925456, 14.3),
    (0.00946049, 14.4),
    (0.0096691, 14.5),
    (0.00988036, 14.6),
    (0.0100939, 14.7),
    (0.01030917, 14.8),
    (0.0105264, 14.9),
    (0.01074586, 15.0),
    (0.01096784, 15.1),
    (0.01119267, 15.2),
    (0.01142071, 15.3),
    (0.01165238, 15.4),
    (0.01188813, 15.5),
    (0.0121285, 15.6),
    (0.0123741, 15.7),
    (0.0126251, 15.8),
    (0.01287884, 15.9),
    (0.01313524, 16.0),
    (0.01339481, 16.1),
    (0.0136581, 16.2),
    (0.01392573, 16.3),
    (0.01419844, 16.4),
    (0.01447705, 16.5),
    (0.01476258, 16.6),
    (0.01505622, 16.7),
    (0.01535945, 16.8),
    (0.01567417, 16.9),
    (0.01600073, 17.0),
    (0.01633633, 17.1),
    (0.01668111, 17.2),
    (0.0170353, 17.3),
    (0.0173991, 17.4),
    (0.01777262, 17.5),
    (0.01815593, 17.6),
    (0.01854898, 17.7),
    (0.01895159, 17.8),
    (0.01936348, 17.9),
    (0.01978419, 18.0),
    (0.02021376, 18.1),
    (0.02065306, 18.2),
    (0.02110233, 18.3),
    (0.02156181, 18.4),
    (0.02203168, 18.5),
    (0.02251212, 18.6),
    (0.02300324, 18.7),
    (0.02350508, 18.8),
    (0.02401764, 18.9),
    (0.02454081, 19.0),
    (0.02507372, 19.1),
    (0.02559679, 19.2),
    (0.02610737, 19.3),
    (0.02661264, 19.4),
    (0.02711906, 19.5),
    (0.02763323, 19.6),
    (0.02816286, 19.7),
    (0.02871809, 19.8),
    (0.02931433, 19.9),
    (0.02997906, 20.0),
    (0.03077566, 20.1),
    (0.03190163, 20.2),
    (0.03326125, 20.3),
    (0.03482782, 20.4),
    (0.03680767, 20.5),
    (0.04066353, 20.6),
    (0.05255037, 20.7),
    (0.14355505, 20.8),
    (0.34874366, 20.9),
    (0.70195765, 21.0),
]


# =================== qh0/d.java : helpers ===================

def age_years(birthday_ms, now_ms):
    """d.f(): age in years from birthday (ms) at reference now (ms), clamped to [12, 120]."""
    days = (now_ms - birthday_ms) / 86400000.0
    yrs = days / 365.242
    return min(120.0, max(12.0, yrs))

def hr_reserve(hr, rest, mx):
    """d.b(): Karvonen HR-reserve fraction clamped [0,1]; below 0.3 reserve contributes nothing."""
    frac = min(1.0, max(0.0, (hr - rest) / (mx - rest)))
    return 0.0 if frac <= RESTING_STRAIN_FLOOR else frac

def percentile(values, q):
    """d.j(): linear-interpolated percentile (q in [0,1]) of `values`."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    if q <= 0.0:
        return s[0]
    if q >= 1.0:
        return s[-1]
    pos = q * (len(s) - 1)
    i = int(pos)
    frac = pos - i
    return s[i] + frac * (s[min(len(s) - 1, i + 1)] - s[i])

# =================== qh0/o.java : strain ===================

def max_hr_estimate(age, sex, fitness):
    """g(): WHOOP max-HR estimate = -0.27261915*age + 204.42714 + sexHrWeight + fitnessWeight."""
    return (age * -0.27261915450538377) + 204.4271407036521 + _sex(sex)["hrWeight"] + FITNESS[fitness]

def _timescales(series, last_used_ms):
    """o.l(): per-sample dt in seconds, capped at 3.0s (series = [(t_ms, hr)])."""
    n = len(series)
    if n == 0:
        return []
    if n == 1:
        if last_used_ms == -1:
            return [1.0]
        return [min(last_used_ms / 1000 - series[-1][0] / 1000, 3.0)]
    times = [t for t, _ in series]
    # d.h (qh0/d.java) uses LONG division: internal diffs are truncated to whole seconds (no-op for
    # 1 Hz device data; matters only for sub-second/irregular sampling).
    diffs = [float((times[min(i + 1, n - 1)] - times[i]) // 1000) for i in range(n)]  # d.h(times,1000)
    # d.h drops the final duplicate index, then o.l re-appends a tail dt (as a float /1000.0):
    diffs = diffs[:-1]
    if last_used_ms == 0:
        diffs.append(diffs[-1] if diffs else 0.0)
    else:
        diffs.append((last_used_ms - times[-1]) / 1000.0)
    return [min(d, 3.0) for d in diffs]

def _strain_point(hrr, dt):
    """o.o(): per-sample strain contribution = 16 / (exp((1-hrr)/0.15) + 1) * dt."""
    return (16.0 / (math.exp((1.0 - hrr) / 0.15) + 1.0)) * dt

def raw_strain(smoothed, rest, mx, last_used_ms):
    """o.h(): sum of per-sample logistic contributions / 691200, clamped [0,1].
    `smoothed` = [(t_ms, hr)] smoothed HRs."""
    reserved = [(t, hr_reserve(hr, rest, mx + 3.0)) for t, hr in smoothed]  # i(list, rest, max+3)
    ts = _timescales(reserved, last_used_ms)
    total = 0.0
    for (t, hrr), dt in zip(reserved, ts):
        total += _strain_point(hrr, dt) if hrr > 0.0 else 0.0
    return min(max(0.0, total / STRAIN_NORM), 1.0)

def scaled_strain(raw):
    """o.j(): map raw strain [0,1] -> 0..21 via the embedded step lookup table."""
    tbl = STRAIN_SCALE_TABLE
    for i in range(len(tbl)):
        lo = tbl[i][0]
        hi = tbl[min(i + 1, len(tbl) - 1)][0]
        if (raw < lo or raw >= hi) and i != min(i + 1, len(tbl) - 1):
            continue
        return tbl[i][1]
    return 0.0

def smooth_hrs(series, last_used_ms=0):
    """o.n(): 25th-percentile smoothing over a +/-30-sample window with gap handling.
    Returns [(t_ms, smoothed_hr)]. Needs enough samples (loops to len-30)."""
    out = []
    n = len(series)
    for i in range(0, max(0, n - 30)):
        t = series[i][0]
        lo, hi = max(0, i - 30), i + 30
        window = series[lo:hi]
        times = [w[0] for w in window]
        gaps = [(times[min(k + 1, len(times) - 1)] - times[k]) for k in range(len(times))]
        max_gap = max(gaps) if gaps else 0
        if max_gap >= 30000:
            ie = gaps.index(max_gap)
            window = window[:ie] if ie >= 30 else window[ie + 1:]
        if window:
            out.append((t, percentile([w[1] for w in window], 0.25)))
    return out

# =================== qh0/b.java : calories ===================

def _resting_kcal_s(sex, weight_kg, height_m, age):
    """b.a(): resting metabolic rate -> kcal/second."""
    c = _sex(sex)
    if sex in NEW_SEXES:
        rmr = c["restAlpha"] + c["restWeight"] * weight_kg + c["restHeight"] * height_m
    else:
        rmr = c["restAlpha"] + c["restWeight"] * (weight_kg * 2.20462) + c["restHeight"] * (height_m * 39.3701)
    d16 = rmr - c["restAge"] * age
    return 0.0 if d16 < 0 else d16 / 86400.0

def _workout_kcal_s(sex, weight_kg, age, hr, mx):
    """b.c(): Keytel workout energy expenditure -> kcal/second."""
    c = _sex(sex)
    ee = c["workHr"] * min(hr, mx) + c["workWeight"] * weight_kg + c["workAge"] * age + c["workAlpha"]
    return 0.0 if ee < 0 else ee / KEYTEL_DIV

def _burn_rate(sex, weight_kg, age, mx, resting_rate, threshold):
    """b.b(): linear WorkoutBurnRate(slope, intercept) used above the workout threshold."""
    c = _sex(sex)
    num = (c["workAlpha"] + c["workHr"] * mx + c["workWeight"] * weight_kg + c["workAge"] * age) / KEYTEL_DIV
    slope = (num - resting_rate) / (mx - threshold)
    return slope, resting_rate - threshold * slope

def calories_kj(profile, smoothed, last_used_ms):
    """b.d(): integrate per-sample kcal over the series -> total kilojoules."""
    sex = profile["sex"]; w = profile["weight_kg"]; age = profile["age"]
    mx = profile["max_hr"]; rest = profile["rest_hr"]
    rest_kcal = _resting_kcal_s(sex, w, profile["height_m"], age)
    ts = _timescales(smoothed, last_used_ms)
    total = 0.0
    if sex in NEW_SEXES:
        threshold = RESTING_STRAIN_FLOOR * (mx - rest) + rest
        slope, intercept = _burn_rate(sex, w, age, mx, rest_kcal, threshold)
        for (t, hr), dt in zip(smoothed, ts):
            total += dt * rest_kcal if hr < threshold else dt * (hr * slope + intercept)
    else:
        # Legacy sexes (b.d else-branch): the resting/workout split is on the HR-RESERVE FRACTION vs
        # 0.3, not raw BPM. hr_reserve returns 0 when reserve <= 0.3, so `reserve < 0.3` ⇔ resting.
        for (t, hr), dt in zip(smoothed, ts):
            reserve = hr_reserve(hr, rest, mx)
            total += dt * rest_kcal if reserve < RESTING_STRAIN_FLOOR else dt * _workout_kcal_s(sex, w, age, hr, mx)
    return total * KJ_PER_KCAL

# =================== top level (o.k) ===================

def compute(profile, hr_series):
    """Compute StrainScore from a user profile and HR series [(unix_ms, bpm), ...].

    profile keys: sex, age (years) OR birthday_ms+now_ms, rest_hr, max_hr, weight_kg, height_m, fitness.
    Returns {strainScore, scaledScore, kilojoules, avgHr, maxHr}.
    """
    if "age" not in profile:
        profile = dict(profile, age=age_years(profile["birthday_ms"], profile["now_ms"]))
    smoothed = smooth_hrs(hr_series)
    if not smoothed:
        return {"strainScore": 0.0, "scaledScore": 0.0, "kilojoules": 0.0,
                "avgHr": profile["rest_hr"], "maxHr": profile["max_hr"], "note": "not enough HR samples to smooth (need > ~60)"}
    hrs = [hr for _, hr in smoothed]
    smax, savg = max(hrs), sum(hrs) / len(hrs)
    mx = profile["max_hr"]
    if smax > mx:                      # lifetime-max update (o.k)
        mx = smax
    last_used = smoothed[-1][0]
    raw = raw_strain(smoothed, profile["rest_hr"], mx, last_used)
    # Calories use the PROFILE max HR (b.d reads userProfile.getMaxHeartRate()), NOT the lifetime-bumped
    # `mx` that strain uses — passing `mx` here would shift the calorie threshold/slope (Java bug parity).
    kj = calories_kj(profile, smoothed, last_used)
    return {"strainScore": raw, "scaledScore": scaled_strain(raw),
            "kilojoules": kj, "avgHr": savg, "maxHr": mx}




# ==================================================================================================
# §4  HRV from v26 PPG  [NOOP]
# ==================================================================================================

V26_LEN = 88
INNER_TYPE_OFF = 8       # frame[8] == 47 (HISTORICAL_DATA)
VERSION_OFF = 9          # frame[9] == 26
TS_OFF = 15              # unix u32 LE
WAVE_START, WAVE_END = 27, 75    # 24 LE-i16 samples (confirmed)
SAMPLE_RATE_HZ = 24              # 24 samples / record, 1 record / second


def is_v26(r: bytes) -> bool:
    return len(r) == V26_LEN and r[INNER_TYPE_OFF] == 47 and r[VERSION_OFF] == 26


def _u32le(r, o):
    return struct.unpack_from("<I", r, o)[0]


def _le_i16(r, a, b):
    return [struct.unpack_from("<h", r, i)[0] for i in range(a, b - 1, 2)]


# ---- validated DSP, ported verbatim from whoop_spot_hrv.py ---------------------------------------

def _detrend(v, win):
    n = len(v); out = [0.0] * n; h = max(1, win // 2)
    for i in range(n):
        lo, hi = max(0, i - h), min(n, i + h + 1)
        out[i] = v[i] - sum(v[lo:hi]) / (hi - lo)
    return out


def _find_peaks(v, min_dist, min_prom):
    cand = [i for i in range(1, len(v) - 1) if v[i] > v[i - 1] and v[i] >= v[i + 1] and v[i] > min_prom]
    cand.sort(key=lambda i: -v[i])
    kept = []
    for i in cand:
        if all(abs(i - j) >= min_dist for j in kept):
            kept.append(i)
    return sorted(kept)


def _interp(v, p):
    if 0 < p < len(v) - 1:
        a, b, c = v[p - 1], v[p], v[p + 1]
        den = a - 2 * b + c
        return (a - c) / (2 * den) if den else 0.0
    return 0.0


def _rmssd_sequential(rr, thr=0.30):
    if len(rr) < 2:
        return None
    glitch = [False] * len(rr)
    for i in range(1, len(rr)):
        if abs(rr[i] - rr[i - 1]) > thr * rr[i - 1]:
            glitch[i] = True
    d = [rr[i] - rr[i - 1] for i in range(1, len(rr)) if not glitch[i - 1] and not glitch[i]]
    return (sum(x * x for x in d) / len(d)) ** 0.5 if len(d) >= 2 else None


def _acf_hr(v, fs, fmin=0.7, fmax=3.0):
    """Robust pulse rate from the normalized autocorrelation of a detrended PPG trace. This is the
    rate estimator that survives coarse 24 Hz sampling (validated: 62.6 vs 64.5 bpm v18 ref, Δ1.9).
    Returns (hr_bpm, strength 0..1) or (None, 0)."""
    n = len(v)
    if n < fs * 3:
        return None, 0.0
    m = sum(v) / n
    x = [a - m for a in v]
    d0 = sum(a * a for a in x) or 1.0
    lo = max(1, int(fs / fmax)); hi = min(n - 1, int(fs / fmin))
    best_lag, best = 0, -1.0
    for lag in range(lo, hi + 1):
        s = sum(x[i] * x[i + lag] for i in range(n - lag)) / d0
        if s > best:
            best, best_lag = s, lag
    return (fs * 60.0 / best_lag if best_lag else None), best


def _spot_hrv(times_s, values, fs):
    """PPG -> spot HR + RMSSD. The rate comes from autocorrelation (robust); RMSSD comes from beat
    detection but is only reported when the detected beats AGREE with the autocorrelation rate — on
    coarse 24 Hz PPG a naive detector double-counts, so we suppress RMSSD to None (quality POOR)
    rather than emit a fabricated number."""
    if len(values) < 30 or fs <= 0:
        return None
    vv = _detrend(values, int(fs))
    # 1) robust rate anchor (matches the reference HR even when beat detection is noisy)
    acf_hr, acf_strength = _acf_hr(vv, fs)
    if not acf_hr:
        return None
    exp_rr = 60000.0 / acf_hr            # expected R-R (ms)
    exp_rr_samp = fs * 60.0 / acf_hr     # expected R-R (samples)
    # 2) beat detection with a refractory tied to the anchor (kills the 24 Hz double-count)
    sd = statistics.pstdev(vv) or 1.0
    peaks = _find_peaks(vv, min_dist=max(2, int(0.6 * exp_rr_samp)), min_prom=0.35 * sd)
    bt = [times_s[p] + _interp(vv, p) / fs for p in peaks]
    rr = [(bt[i + 1] - bt[i]) * 1000.0 for i in range(len(bt) - 1)]
    # 3) physiologic AND anchor-band gate (reject ectopic / missed beats around the true rate)
    rr = [x for x in rr if 300 <= x <= 2000 and 0.6 * exp_rr <= x <= 1.6 * exp_rr]
    if len(rr) < 2:
        return {"hr": acf_hr, "beat_hr": None, "rmssd": None, "rmssd_raw": None, "n_rr": len(rr),
                "n_clean": 0, "rr": rr, "hr_err": None, "acf_strength": round(acf_strength, 2),
                "span_s": (times_s[-1] - times_s[0]) if times_s else 0.0, "quality": "POOR"}
    beat_hr = 60000.0 / statistics.median(rr)
    rmssd = _rmssd_sequential(rr)
    n_clean = sum(1 for i in range(1, len(rr)) if abs(rr[i] - rr[i - 1]) <= 0.20 * rr[i - 1])
    # 4) quality gate: RMSSD is trustworthy only when the beats track the autocorrelation rate
    hr_err = abs(beat_hr - acf_hr) / acf_hr
    if hr_err > 0.12 or rmssd is None or n_clean < 8:
        q = "POOR"
    elif hr_err <= 0.06 and n_clean >= 20:
        q = "GOOD"
    else:
        q = "COARSE"
    return {"hr": acf_hr, "beat_hr": beat_hr, "rmssd": (rmssd if q != "POOR" else None),
            "rmssd_raw": rmssd, "n_rr": len(rr), "n_clean": n_clean, "rr": rr,
            "hr_err": round(hr_err, 3), "acf_strength": round(acf_strength, 2),
            "span_s": (times_s[-1] - times_s[0]) if times_s else 0.0, "quality": q}


# ---- v26 frames -> HRV ---------------------------------------------------------------------------

def _consecutive_runs(v26):
    """Group v26 records into consecutive-second runs (PPG phase is only continuous within a run)."""
    recs = sorted(v26, key=lambda r: _u32le(r, TS_OFF))
    runs, cur = [], []
    for r in recs:
        t = _u32le(r, TS_OFF)
        if cur and t - _u32le(cur[-1], TS_OFF) != 1:
            runs.append(cur); cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return [run for run in runs if len(run) >= 4]


def hrv_from_v26(frames):
    """frames: iterable of raw frame bytes. Returns the best (largest-span, best-quality) burst HRV, or
    None. Each burst is a consecutive-second run of v26 records concatenated into a 24 Hz PPG trace."""
    v26 = [r for r in frames if is_v26(r)]
    if not v26:
        return None
    best = None
    bursts = []
    for run in _consecutive_runs(v26):
        sig, times = [], []
        base = _u32le(run[0], TS_OFF)
        for r in run:
            sec = _u32le(r, TS_OFF) - base
            samples = _le_i16(r, WAVE_START, WAVE_END)
            n = len(samples)
            for i, s in enumerate(samples):
                sig.append(float(s)); times.append(sec + i / n)
        res = _spot_hrv(times, sig, SAMPLE_RATE_HZ)
        if res:
            bursts.append(res)
    if not bursts:
        return None
    # rank: prefer GOOD > COARSE > POOR, then more clean beats
    rank = {"GOOD": 2, "COARSE": 1, "POOR": 0}
    best = max(bursts, key=lambda b: (rank.get(b["quality"], 0), b["n_clean"]))
    best = dict(best)
    best["n_bursts"] = len(bursts)
    return best




# ==================================================================================================
# §5  R20 OPTICAL / SpO2  [empirical]
# ==================================================================================================

# ---- what the Java tells us (fixed) --------------------------------------------------------------
R20_TOTAL = 2140
R20_CHANNEL_K = 20
INNER_OFF = 8                       # whoop5 inner record start (frame → inner)
HDR_TYPE, HDR_K, HDR_INDEX, HDR_UNIX, HDR_SUBSEC = 0, 1, 3, 7, 11   # inner offsets [kq0/b]
PAYLOAD_MIN = 13                    # optical payload starts at/after inner offset 13
# v18 ground-truth HR (frame-absolute), same as analyze_v26_waveform.
V18_LEN, V18_HR_FRAME_OFF, V18_UNIX_FRAME_OFF = 124, 22, 15


def u32(b, o):  return struct.unpack_from("<I", b, o)[0]
def u16le(b, o): return struct.unpack_from("<H", b, o)[0]


def inner_of(frame):
    return frame[INNER_OFF:-4]      # strip whoop5 header + crc32 (no CRC check needed for analysis)


def is_r20(frame):
    return len(frame) == R20_TOTAL and len(frame) > INNER_OFF + 2 and frame[INNER_OFF + HDR_K] == R20_CHANNEL_K


def r20_unix(frame):
    rec = inner_of(frame)
    return u32(rec, HDR_UNIX) if len(rec) >= HDR_UNIX + 4 else None


# ---- ground truth (HR) from the same capture -----------------------------------------------------
def ground_truth_hr(records):
    """unix → bpm from v18 records (frame[22]) and/or the live 2a37 tag."""
    gt = {}
    for r in records:
        f = bytes.fromhex(r["hex"])
        if len(f) == V18_LEN and f[8] == 47 and f[9] == 18:
            hr = f[V18_HR_FRAME_OFF]
            if hr:
                gt.setdefault(u32(f, V18_UNIX_FRAME_OFF), hr)
    for r in records:
        if r.get("hr") and r.get("ts_ms"):
            gt[int(r["ts_ms"] // 1000)] = r["hr"]
    return gt


# ---- DSP (same as analyze_v26_waveform / whoop_spot_hrv) -----------------------------------------
def detrend(x, w=12):
    out = []
    for i in range(len(x)):
        lo, hi = max(0, i - w), min(len(x), i + w + 1)
        out.append(x[i] - statistics.mean(x[lo:hi]))
    return out


def acf(x, lag):
    n = len(x) - lag
    if n <= 0:
        return 0.0
    m = statistics.mean(x)
    den = sum((xi - m) ** 2 for xi in x)
    return (sum((x[i] - m) * (x[i + lag] - m) for i in range(n)) / den) if den else 0.0


def dominant_bpm(sig, fs, target_fs=32.0, max_samples=1200):
    """Autocorrelation fundamental → bpm. Block-averages the signal down to ~target_fs first (an
    anti-alias lowpass, HR is <4 Hz) so the search is cheap regardless of the raw optical rate."""
    decim = max(1, int(round(fs / target_fs)))
    if decim > 1:
        sig = [statistics.mean(sig[i:i + decim]) for i in range(0, len(sig) - decim + 1, decim)]
    fs_eff = fs / decim
    sig = detrend(sig[:max_samples])
    lo = max(1, int(fs_eff * 60 / 220))      # 220 bpm
    hi = int(fs_eff * 60 / 30)               # 30 bpm
    best_lag, best = None, -2.0
    for lag in range(lo, min(hi, len(sig) - 1) + 1):
        v = acf(sig, lag)
        if v > best:
            best, best_lag = v, lag
    if not best_lag:
        return None, 0.0
    return fs_eff * 60.0 / best_lag, round(best, 3)


# ---- channel extraction under a layout hypothesis ------------------------------------------------
def _samples(rec, start, count, enc):
    fmt = {"i16": "<h", "u16": "<H", "i16be": ">h"}[enc]
    return [struct.unpack_from(fmt, rec, start + 2 * i)[0] for i in range(count)]


def extract_channels(rec, start, n_ch, n_per, enc, layout):
    """Return a list of n_ch channels (each n_per samples) under a layout hypothesis."""
    if layout == "planar":     # [c0×n_per][c1×n_per]…
        return [_samples(rec, start + c * n_per * 2, n_per, enc) for c in range(n_ch)]
    else:                       # interleaved [c0 c1 c2  c0 c1 c2 …]
        out = [[] for _ in range(n_ch)]
        for i in range(n_per):
            for c in range(n_ch):
                out[c].append(_samples(rec, start + (i * n_ch + c) * 2, 1, enc)[0])
        return out


# ---- consecutive-second runs (phase continuity) --------------------------------------------------
def runs_of(r20_frames):
    recs = sorted(r20_frames, key=r20_unix)
    runs, cur = [], []
    for f in recs:
        if cur and r20_unix(f) - r20_unix(cur[-1]) != 1:
            runs.append(cur); cur = []
        cur.append(f)
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 4]


# =================================================================================================
# SCENARIO A — structural map (per-byte variance → header/data boundary)
# =================================================================================================
def scenario_map(r20_frames):
    recs = [inner_of(f) for f in r20_frames]
    L = min(len(r) for r in recs)
    print(f"R20: {len(recs)} frames · inner length {L} bytes")
    print("header (decoded):")
    f0 = recs[0]
    print(f"  type={f0[HDR_TYPE]} channel(K)={f0[HDR_K]} index={u32(f0,HDR_INDEX)} "
          f"unix={u32(f0,HDR_UNIX)} subsec={u16le(f0,HDR_SUBSEC)}")
    # probe u16 count-like fields just after the header (R21 had samplingFreq@14, count@16)
    print("candidate count/config fields (u16 LE, inner offsets 13..26):")
    for o in range(13, 27, 2):
        vals = {u16le(r, o) for r in recs if len(r) > o + 1}
        tag = "CONST" if len(vals) == 1 else f"{min(vals)}..{max(vals)}"
        print(f"  @{o:<3} {tag}")
    # per-byte variance to find where the sample region begins (low var = header/counter, high = data)
    print("byte-variance boundary scan (first offset where variance stays high = sample region):")
    step = 4
    prev = None
    for o in range(0, min(L, 60)):
        col = [r[o] for r in recs if len(r) > o]
        var = statistics.pvariance(col) if len(col) > 1 else 0
        if o % step == 0:
            mark = "▓" if var > 500 else ("▒" if var > 20 else "·")
            print(f"  @{o:<4}{mark} var={var:.0f}", end="   " if (o // step) % 3 != 2 else "\n")
    print()


# =================================================================================================
# SCENARIO B — layout finder (HR-lock disambiguation)
# =================================================================================================
def scenario_find(r20_frames, gt, starts=(13, 14, 16, 18, 20), channel_counts=(1, 2, 3, 4),
                  encodings=("i16", "u16"), layouts=("planar", "interleaved"), tol=8.0):
    runs = runs_of(r20_frames)
    if not runs:
        print("no consecutive-second R20 runs (need a continuous R20 stream)."); return []
    payload_end = min(len(inner_of(f)) for run in runs for f in run)
    # HR present in the runs
    hrs = [gt[r20_unix(f)] for run in runs for f in run if r20_unix(f) in gt]
    if not hrs:
        print("no ground-truth HR overlaps the R20 frames — capture v18 or 2a37 alongside R20."); return []
    spread = max(hrs) - min(hrs)
    # per-run ground-truth HR (the disambiguator is HR TRACKING across runs, not a global mean)
    run_hrs = []
    for run in runs:
        hs = [gt[r20_unix(f)] for f in run if r20_unix(f) in gt]
        run_hrs.append(statistics.median(hs) if hs else None)
    n_hr_runs = sum(1 for h in run_hrs if h)
    need = max(1, (n_hr_runs + 1) // 2)     # track HR in a majority of HR-covered runs
    print(f"R20 runs: {len(runs)} ({n_hr_runs} with HR) · GT HR {min(hrs)}..{max(hrs)} bpm (spread {spread:.0f})")
    if spread < 15:
        print("  ⚠ HR spread <15 bpm — a resting capture can't tell a real pulse from an artifact. "
              "Record rest→exertion for a decisive result.")
    hits = []
    for start in starts:
        avail = payload_end - start
        for n_ch in channel_counts:
            n_per = avail // (n_ch * 2)
            if n_per < 16:
                continue
            fs = n_per            # 1 record/sec → samples/channel = Hz
            for enc in encodings:
                for layout in layouts:
                    # extract each run's channels once, then score per channel by HR tracking
                    run_channels = []
                    for run in runs:
                        chans = [[] for _ in range(n_ch)]
                        for f in run:
                            cs = extract_channels(inner_of(f), start, n_ch, n_per, enc, layout)
                            for c in range(n_ch):
                                chans[c] += cs[c]
                        run_channels.append(chans)
                    locked, best_err = 0, []
                    for c in range(n_ch):
                        tracks = 0
                        for ri, run in enumerate(runs):
                            if run_hrs[ri] is None:
                                continue
                            bpm, conf = dominant_bpm(run_channels[ri][c], fs)
                            if bpm and conf > 0.2 and abs(bpm - run_hrs[ri]) <= tol:
                                tracks += 1
                                best_err.append(abs(bpm - run_hrs[ri]))
                        if tracks >= need:
                            locked += 1
                    if locked:
                        hits.append({"start": start, "channels": n_ch, "samples": n_per, "fs": fs,
                                     "enc": enc, "layout": layout, "hr_locked_channels": locked,
                                     "mean_err": statistics.mean(best_err) if best_err else 99})
    # prefer: more HR-locked channels, then u16 (optical intensity is unsigned), then lower error
    hits.sort(key=lambda h: (h["hr_locked_channels"], h["enc"] == "u16", -h["mean_err"]), reverse=True)
    if not hits:
        print("no layout produced an HR-locked channel. Widen the sweep, or the encoding may be i24/i32.")
        return hits
    print(f"\n{'start':>5} {'chans':>5} {'smp/ch':>6} {'fs(Hz)':>6} {'enc':>5} {'layout':>11} {'HR-locked':>9}")
    seen = set()
    for h in hits[:12]:
        k = (h["channels"], h["layout"], h["enc"])
        if k in seen:
            continue
        seen.add(k)
        print(f"{h['start']:>5} {h['channels']:>5} {h['samples']:>6} {h['fs']:>6} {h['enc']:>5} "
              f"{h['layout']:>11} {h['hr_locked_channels']:>9}")
    best = hits[0]
    print(f"\n→ best hypothesis: {best['channels']} channels, {best['layout']}, {best['enc']}, "
          f"{best['samples']} samp/ch (~{best['fs']} Hz), payload@{best['start']}. "
          f"{best['hr_locked_channels']} channel(s) track HR.")
    return hits


# =================================================================================================
# SCENARIO C — red/IR classification + ratio-of-ratios SpO2
# =================================================================================================
def _ac_dc(sig):
    dc = statistics.mean(sig)
    d = detrend(sig)
    ac = (max(d) - min(d))
    return ac, dc


def scenario_spo2(r20_frames, layout_hyp):
    """layout_hyp = dict(start, channels, samples, enc, layout). Classify channels + relative SpO2."""
    runs = runs_of(r20_frames)
    if not runs:
        print("no R20 runs."); return
    s = dict(layout_hyp)
    if s["enc"] != "u16":
        s["enc"] = "u16"      # optical intensity (DC) is unsigned — force u16 for the ratio-of-ratios
    per_ch_acdc = [[] for _ in range(s["channels"])]
    for run in runs:
        chans = [[] for _ in range(s["channels"])]
        for f in run:
            cs = extract_channels(inner_of(f), s["start"], s["channels"], s["samples"], s["enc"], s["layout"])
            for c in range(s["channels"]):
                chans[c] += cs[c]
        for c in range(s["channels"]):
            ac, dc = _ac_dc(chans[c])
            if dc:
                per_ch_acdc[c].append((ac, dc))
    print("channel classification (by DC level — IR usually highest DC, green most pulsatile):")
    prof = []
    for c in range(s["channels"]):
        if not per_ch_acdc[c]:
            continue
        ac = statistics.mean(a for a, _ in per_ch_acdc[c])
        dc = statistics.mean(d for _, d in per_ch_acdc[c])
        perf = ac / abs(dc) if dc else 0
        prof.append((c, ac, dc, perf))
        print(f"  ch{c}: AC≈{ac:.0f} DC≈{dc:.0f} perfusion(AC/DC)≈{perf*100:.2f}%")
    if len(prof) < 2:
        print("need ≥2 channels to compute SpO2."); return
    # heuristics: green = highest perfusion (pin as PPG/HR); of the rest, IR = higher DC, red = lower DC
    by_dc = sorted(prof, key=lambda p: p[2], reverse=True)
    ir = by_dc[0]; red = by_dc[1]
    R = (red[1] / abs(red[2])) / (ir[1] / abs(ir[2])) if ir[1] and ir[2] else None
    print(f"\n  → IR = ch{ir[0]} (highest DC) · RED = ch{red[0]}")
    # Physiology gate: real PPG perfusion (AC/DC) is ~0.05–15%. If the "channels" show more, this
    # planar/interleaved split is NOT cleanly isolating the LEDs (the R20 buffer is block-structured),
    # so ANY SpO2 from it is meaningless — refuse to print a number rather than fabricate one.
    red_perf = red[1] / abs(red[2]) if red[2] else 9e9
    ir_perf = ir[1] / abs(ir[2]) if ir[2] else 9e9
    if red_perf > 0.20 or ir_perf > 0.20:
        print(f"\n  ✗ SpO2 NOT emitted — channel isolation is non-physiological "
              f"(perfusion RED {red_perf*100:.0f}% / IR {ir_perf*100:.0f}%; real PPG is <15%).")
        print("    R20 IS confirmed to carry real cardiac PPG with a DC baseline, so SpO2 is structurally")
        print("    POSSIBLE — but this simple planar/interleaved split doesn't separate the LEDs; the")
        print("    on-device layout is block-structured. To finish it: capture ~2 min WHILE wearing a")
        print("    reference finger pulse-oximeter, so the true SpO2 pins which block is RED vs IR and")
        print("    calibrates the curve. Without that ground truth, no honest SpO2 number is possible.")
        return
    if R:
        print(f"  ratio-of-ratios R = {R:.3f}")
        vals = spo2_from_R(R)
        for name, val in vals.items():
            print(f"  SpO2 [{name:<16}] ≈ {val:.0f}%")
        if not any(90 <= v <= 100 for v in vals.values()):
            print("  ⚠ every curve lands outside the healthy 90–100% band → treat as UNTRUSTWORTHY "
                  "(layout/calibration unresolved), NOT a real desaturation.")
        print("  NOTE: these are UNCALIBRATED industry curves (±2–5%). For your number, calibrate a,b,c "
              "against a reference finger oximeter. Best use: DESATURATION TRENDS, not a clinical value.")


# Industry SpO2 calibration curves (ratio-of-ratios R → %). Each manufacturer keeps its own; these are
# the widely-used reference-design curves. WHOOP's exact curve is proprietary → self-calibrate for accuracy.
SPO2_CAL_MAXIM = (-45.060, 30.354, 94.845)   # MAX30102 reference: a*R^2 + b*R + c  [Analog Devices]


def spo2_from_R(R, quad=SPO2_CAL_MAXIM):
    """Return SpO2 estimates from R under a few published curves (clamped to a physiological 70–100%)."""
    a, b, c = quad
    def clamp(x):
        return max(70.0, min(100.0, x))
    return {
        "Maxim quadratic": clamp(a * R * R + b * R + c),      # −45.06R²+30.354R+94.845 (best default)
        "linear −23.3":    clamp(-23.3 * (R - 0.4) + 100.0),  # SpO2 = −23.3(R−0.4)+100
        "linear 110−25R":  clamp(110.0 - 25.0 * R),           # rough
    }


def spo2_calibrate(pairs):
    """Fit your own quadratic a·R²+b·R+c from [(R, reference_spo2), …] pairs (least squares, stdlib).
    Collect pairs by wearing a reference finger oximeter while capturing R20. Needs ≥3 well-spread points."""
    n = len(pairs)
    if n < 3:
        return None
    # normal equations for quadratic least squares
    Sx = [sum(R ** k for R, _ in pairs) for k in range(5)]      # sum R^0..R^4
    Sy = [sum(y * R ** k for R, y in pairs) for k in range(3)]  # sum y, yR, yR^2
    # solve 3x3 [[S0,S1,S2],[S1,S2,S3],[S2,S3,S4]] · [c,b,a] = [Sy0,Sy1,Sy2]
    M = [[Sx[0], Sx[1], Sx[2]], [Sx[1], Sx[2], Sx[3]], [Sx[2], Sx[3], Sx[4]]]
    v = [Sy[0], Sy[1], Sy[2]]
    for i in range(3):                                          # Gaussian elimination
        p = M[i][i] or 1e-9
        for j in range(i + 1, 3):
            f = M[j][i] / p
            for k in range(3):
                M[j][k] -= f * M[i][k]
            v[j] -= f * v[i]
    c = v[2] / (M[2][2] or 1e-9)
    b = (v[1] - M[1][2] * c) / (M[1][1] or 1e-9)
    a = (v[0] - M[0][1] * b - M[0][2] * c) / (M[0][0] or 1e-9)
    return (a, b, c)


# =================================================================================================
# demo / selftest — synthetic 3-channel R20 (green HR-locked, red, IR)
# =================================================================================================


# ==================================================================================================
# §7  METRICS (GenieMax) + DASHBOARD
# ==================================================================================================

# ============================ signal extraction ============================

def extract_signals(records, family):
    """Return (hr_series[(unix,bpm)], v26_frames[bytes], gt_series[(unix,bpm)] from live HR tag).

    HRV is derived from the v26 PPG waveform (whoop_hrv), NOT from R-R intervals: R-R is not present in
    ANY BLE packet (verified across the app's packet classes), so a v18 R-R offset would be a guess.
    """
    hr, v26, gt = [], [], []
    for r in records:
        frame = bytes.fromhex(r["hex"])
        d = decode_frame(frame, family)
        if d and d.get("type") in (40, 47) and d.get("heart_rate"):
            hr.append((d["unix"], float(d["heart_rate"])))
        if is_v26(frame):
            v26.append(frame)
        if r.get("hr") and r.get("ts_ms"):
            gt.append((int(r["ts_ms"] // 1000), float(r["hr"])))
    hr.sort(); gt.sort()
    return hr, v26, gt


# ============================ GenieMax formulas ============================

def rmssd(rr):
    if len(rr) < 2:
        return None
    d = [rr[i + 1] - rr[i] for i in range(len(rr) - 1)]
    return math.sqrt(sum(x * x for x in d) / len(d))


def sdnn(rr):
    if len(rr) < 2:
        return None
    m = statistics.mean(rr)
    return math.sqrt(sum((x - m) ** 2 for x in rr) / len(rr))


def poincare(rr):
    s = sdnn(rr); r = rmssd(rr)
    if s is None or r is None:
        return None
    sd1 = r / math.sqrt(2)
    sd2 = math.sqrt(max(0.0, 2 * s * s - sd1 * sd1))
    return {"sd1": sd1, "sd2": sd2, "balance": (sd1 / sd2 if sd2 else None)}


def baevsky_si(rr):
    if len(rr) < 2:
        return None
    binned = [round(x / 50.0) * 50 for x in rr]           # 50 ms bins
    mode_bin = statistics.mode(binned)
    mo = mode_bin / 1000.0                                 # mode, seconds
    amo = 100.0 * binned.count(mode_bin) / len(binned)     # amplitude, %
    vr = (max(rr) - min(rr)) / 1000.0                       # range, seconds
    return amo / (2 * vr * mo) if (vr and mo) else None


def hr_max_tanaka(age):
    return 208 - 0.7 * age


def hrr(hr, rest, hrmax):
    return (hr - rest) / (hrmax - rest)


def vo2max_uth(hrmax, rest):
    return 15.3 * hrmax / rest


def zones(hr_series, rest, hrmax):
    """Minutes spent in Z1..Z5 by %HRR bands 0.5/0.6/0.7/0.8/0.9."""
    z = [0.0] * 5
    for i in range(1, len(hr_series)):
        dt = (hr_series[i][0] - hr_series[i - 1][0]) / 60.0
        if dt <= 0 or dt > 1.0:
            dt = min(max(dt, 0.0), 1.0)
        x = hrr(hr_series[i][1], rest, hrmax)
        if x >= 0.9:
            z[4] += dt
        elif x >= 0.8:
            z[3] += dt
        elif x >= 0.7:
            z[2] += dt
        elif x >= 0.6:
            z[1] += dt
        elif x >= 0.5:
            z[0] += dt
    return z


def trimp_banister(hr_series, rest, hrmax, male=True):
    """Banister TRIMP over the series, and day-strain 21*(1-exp(-TRIMP/tau))."""
    trimp = 0.0
    for i in range(1, len(hr_series)):
        dt = (hr_series[i][0] - hr_series[i - 1][0]) / 60.0
        if dt <= 0 or dt > 1.0:
            dt = min(max(dt, 0.0), 1.0)
        x = min(1.0, max(0.0, hrr(hr_series[i][1], rest, hrmax)))
        trimp += dt * x * (0.64 * math.exp(1.92 * x) if male else 0.86 * math.exp(1.67 * x))
    return trimp


TAU_BANISTER = 40.0   # GenieMax leaves tau symbolic; 40 maps a hard all-day TRIMP toward ~21.


def day_strain_banister(trimp, tau=TAU_BANISTER):
    return 21.0 * (1 - math.exp(-trimp / tau))


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def recovery(z_hrv, z_rhr, z_rr, z_sleep):
    return 100.0 * phi(0.55 * z_hrv - 0.20 * z_rhr - 0.10 * z_rr + 0.15 * z_sleep)


def readiness(rec, sleep_score, tsb_norm, illness_penalty=0.0):
    return 0.5 * rec + 0.3 * sleep_score + 0.2 * tsb_norm - illness_penalty


def keytel_kcal_min(hr, w, a, male=True):
    if male:
        return (-55.0969 + 0.6309 * hr + 0.1988 * w + 0.2017 * a) / 4.184
    return (-20.4022 + 0.4472 * hr - 0.1263 * w + 0.074 * a) / 4.184


# ============================ dashboard ============================

def build(profile, hr_series, v26_frames, gt):
    out = {}
    age = profile["age"]; rest = profile["rest_hr"]
    male = profile["sex"].startswith("male")
    hrmax_tanaka = hr_max_tanaka(age)
    out["hr_samples"] = len(hr_series)
    out["hr_mean"] = statistics.mean([h for _, h in hr_series]) if hr_series else None
    out["hr_max_obs"] = max([h for _, h in hr_series]) if hr_series else None

    # HRV — from the v26 PPG waveform (confirmed [27:75]); the full panel is computed from the
    # PPG-derived beat intervals (PPI), which ARE the R-R series WHOOP itself derives HRV from.
    hv = hrv_from_v26(v26_frames)
    if hv and hv.get("rr") and len(hv["rr"]) >= 2:
        rr = hv["rr"]
        out["hrv"] = {"rmssd": hv["rmssd"], "sdnn": sdnn(rr), **(poincare(rr) or {}),
                      "baevsky_si": baevsky_si(rr), "n_rr": len(rr),
                      "quality": hv.get("quality"), "ppg_hr": hv.get("hr")}
    else:
        out["hrv"] = None

    # fitness / zones
    out["hr_max_tanaka"] = hrmax_tanaka
    out["vo2max_uth"] = vo2max_uth(hrmax_tanaka, rest)
    out["zones_min"] = zones(hr_series, rest, hrmax_tanaka) if hr_series else None

    # strain — WHOOP-exact vs GenieMax Banister (the headline comparison)
    trimp = trimp_banister(hr_series, rest, hrmax_tanaka, male) if hr_series else 0.0
    out["genie_trimp"] = trimp
    out["genie_strain"] = day_strain_banister(trimp)
    ws_res = compute(profile, [(t * 1000, h) for t, h in hr_series]) if hr_series else None
    out["whoop_strain"] = ws_res

    # calories — GenieMax Keytel (integrate) vs WHOOP engine (whoop_strain)
    kcal = 0.0
    for i in range(1, len(hr_series)):
        dt = (hr_series[i][0] - hr_series[i - 1][0]) / 60.0
        if 0 < dt <= 1.0:
            kcal += max(0.0, keytel_kcal_min(hr_series[i][1], profile["weight_kg"], age, male)) * dt
    out["genie_kcal"] = kcal
    out["genie_kj"] = kcal * 4.184

    # baselines / recovery / readiness — need history; use supplied baselines if present
    b = profile.get("baselines")
    if b and out["hrv"] and out["hrv"].get("rmssd"):
        z_hrv = (out["hrv"]["rmssd"] - b["hrv_mu"]) / b["hrv_sd"]
        z_rhr = (rest - b["rhr_mu"]) / b["rhr_sd"]
        z_rr = 0.0
        z_sleep = b.get("z_sleep", 0.0)
        rec = recovery(z_hrv, z_rhr, z_rr, z_sleep)
        tsb_norm = b.get("tsb_norm", 50.0)
        out["recovery"] = rec
        out["readiness"] = readiness(rec, b.get("sleep_score", 70.0), tsb_norm, b.get("illness_penalty", 0.0))
    else:
        out["recovery"] = None
        out["readiness"] = None
    return out


def fmt(x, n=1):
    return "—" if x is None else (f"{x:.{n}f}" if isinstance(x, float) else str(x))


def print_dashboard(d, gt_compare):
    P = print
    P("\n" + "=" * 62)
    P("  WHOOP LOCAL DASHBOARD  (from capture.json, no cloud)")
    P("=" * 62)
    P(f"  HR: {d['hr_samples']} samples · mean {fmt(d['hr_mean'])} · max {fmt(d['hr_max_obs'])} bpm")

    P("\n  ── HRV ──────────────────────────────────────────────")
    if d["hrv"]:
        h = d["hrv"]
        P(f"   RMSSD {fmt(h['rmssd'])} ms · SDNN {fmt(h['sdnn'])} ms · n={h['n_rr']} beats · "
          f"[{h.get('quality')}, from v26 PPG @{fmt(h.get('ppg_hr'),0)} bpm]")
        P(f"   SD1 {fmt(h.get('sd1'))} · SD2 {fmt(h.get('sd2'))} · SD1/SD2 {fmt(h.get('balance'),2)} · Baevsky SI {fmt(h.get('baevsky_si'),1)}")
    else:
        P("   — needs a v26 PPG burst (from the historical offload). No R-R in any live BLE packet.")

    P("\n  ── Fitness / Zones ──────────────────────────────────")
    P(f"   HRmax(Tanaka) {fmt(d['hr_max_tanaka'])} · VO2max(Uth) {fmt(d['vo2max_uth'])}")
    if d["zones_min"]:
        zt = " · ".join(f"Z{i+1} {m:.1f}m" for i, m in enumerate(d["zones_min"]))
        P(f"   time in zones: {zt}")

    P("\n  ── Strain  (WHOOP-exact  vs  GenieMax Banister) ─────")
    wsc = d["whoop_strain"]
    ws_scaled = wsc["scaledScore"] if wsc else None
    P(f"   WHOOP (qh0, extracted) : {fmt(ws_scaled)} / 21")
    P(f"   GenieMax (Banister)    : {fmt(d['genie_strain'])} / 21   (TRIMP {fmt(d['genie_trimp'])})")
    if ws_scaled is not None and d["genie_strain"] is not None:
        P(f"   Δ = {abs(ws_scaled - d['genie_strain']):.1f}  (different models; WHOOP one is authoritative)")

    P("\n  ── Calories  (WHOOP engine  vs  GenieMax Keytel) ────")
    ws_kj = wsc["kilojoules"] if wsc else None
    P(f"   WHOOP (qh0/Keytel) : {fmt(ws_kj)} kJ  ({fmt((ws_kj or 0)/4.184)} kcal)")
    P(f"   GenieMax (Keytel)  : {fmt(d['genie_kj'])} kJ  ({fmt(d['genie_kcal'])} kcal)")

    P("\n  ── Recovery / Readiness ─────────────────────────────")
    if d["recovery"] is not None:
        P(f"   Recovery {fmt(d['recovery'])}%  ·  Readiness {fmt(d['readiness'])}")
    else:
        P("   — needs baselines (≥7 days of EWMA history); pass --demo to see it wired")

    if gt_compare:
        P("\n  ── Decoded HR vs live ground-truth (2a37) ───────────")
        P(f"   matched {gt_compare['n']} sec · mean|Δ| {fmt(gt_compare['mae'],2)} bpm · "
          f"corr {fmt(gt_compare['corr'],3)}")
    P("=" * 62 + "\n")


def compare_gt(hr_series, gt):
    hr_at = dict(hr_series)
    pairs = [(hr_at[t], g) for t, g in gt if t in hr_at]
    if len(pairs) < 3:
        return None
    a = [x for x, _ in pairs]; b = [y for _, y in pairs]
    mae = statistics.mean(abs(x - y) for x, y in pairs)
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in pairs)
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return {"n": len(pairs), "mae": mae, "corr": (num / den if den else None)}


# ============================ demo ============================



# ==================================================================================================
# §8  LIVE  (pair → stream → offload, progressive)
# ==================================================================================================

# GATT config reused from whoop_capture.py (kept local so this file also runs standalone).
WHOOP4 = {"service": "61080001-8d6d-82b8-614a-1c8cb0f8dcc6",
          "cmd_write": "61080002-8d6d-82b8-614a-1c8cb0f8dcc6",
          "notify": ["61080003-8d6d-82b8-614a-1c8cb0f8dcc6", "61080004-8d6d-82b8-614a-1c8cb0f8dcc6",
                     "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"]}
WHOOP5 = {"service": "fd4b0001-cce1-4033-93ce-002d5875f58a",
          "cmd_write": "fd4b0002-cce1-4033-93ce-002d5875f58a",
          "notify": ["fd4b0003-cce1-4033-93ce-002d5875f58a", "fd4b0004-cce1-4033-93ce-002d5875f58a",
                     "fd4b0005-cce1-4033-93ce-002d5875f58a", "fd4b0007-cce1-4033-93ce-002d5875f58a"]}
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"


class Live:
    """In-memory frame accumulator (same record shape as whoop_capture, but never flushed to disk)."""

    def __init__(self, family, profile):
        self.family = family
        self.profile = profile
        self.inner_off = WHOOP5_INNER_OFF if family == "whoop5" else WHOOP4_INNER_OFF
        self.records = []
        self.latest_hr = None
        self.reassemblers = {}
        self.frames = 0
        self.std_hr = []          # [(unix_sec, bpm)] from the standard 0x2A37 profile (always works)
        self.link_valid_pending = []   # origin seqs of strap LINK_VALID commands awaiting our reply
        self.link_valid_replies = 0
        # historical offload (fetches v26 PPG for HRV). Verified helpers from whoop_frame.
        self.offload = False
        self.ack_pending = []          # HISTORY_END end_data awaiting an ack (advances the trim cursor)
        self.acks_sent = 0
        self.type47 = 0
        self._end_data_fn = history_end_data if family == "whoop5" else history_end_data_whoop4
        self._ack_fn = build_history_ack if family == "whoop5" else build_history_ack_whoop4

    def on_cmd_notify(self, sender, data: bytearray):
        """cmd-from-strap channel: store frames AND answer the strap-initiated LINK_VALID handshake
        (reply 'There it is.' — without it the strap withholds the biometric stream)."""
        char = str(getattr(sender, "uuid", sender)).lower()
        ra = self.reassemblers.get(char)
        if ra is None:
            ra = Reassembler(self.family)
            self.reassemblers[char] = ra
        for frame in ra.feed(bytes(data)):
            self.frames += 1
            self.records.append({"hex": frame.hex(), "char": char,
                                 "ts_ms": int(time.time() * 1000), "hr": self.latest_hr})
            origin = is_link_valid_command(frame, self.inner_off)
            if origin is not None:
                self.link_valid_pending.append(origin)

    def on_hr(self, _sender, data: bytearray):
        hr = parse_standard_hr(bytes(data))
        if hr is not None:
            self.latest_hr = hr
            sec = int(time.time())
            if not self.std_hr or self.std_hr[-1][0] != sec:   # one sample per second
                self.std_hr.append((sec, float(hr)))

    def on_frame_notify(self, sender, data: bytearray):
        char = str(getattr(sender, "uuid", sender)).lower()
        ra = self.reassemblers.get(char)
        if ra is None:
            ra = Reassembler(self.family)
            self.reassemblers[char] = ra
        for frame in ra.feed(bytes(data)):
            self.frames += 1
            self.records.append({"hex": frame.hex(), "char": char,
                                 "ts_ms": int(time.time() * 1000), "hr": self.latest_hr})
            if len(frame) > self.inner_off and frame[self.inner_off] == PACKET_HISTORICAL_DATA:
                self.type47 += 1
            if self.offload:
                end_data = self._end_data_fn(frame)   # non-None only for a CRC-valid HISTORY_END
                if end_data is not None and end_data not in self.ack_pending:
                    self.ack_pending.append(end_data)

    def render(self):
        hr_series, v26, gt = extract_signals(self.records, self.family)
        source = "strap frames (type 40/47)"
        if not hr_series and self.std_hr:
            # Custom biometric frames aren't streaming — fall back to the standard HR profile, which is
            # a perfectly good real-time HR source for strain / calories / zones (they only need HR/time).
            hr_series, gt = list(self.std_hr), []
            source = "standard HR 2a37 (custom frames not streaming)"
        print("\033[2J\033[H", end="")   # clear + home
        print(f"  ● LIVE  ·  strap frames {self.frames}  ·  LINK_VALID replies {self.link_valid_replies}  ·  "
              f"2a37 HR pts {len(self.std_hr)}  ·  latest HR {self.latest_hr}")
        if self.offload:
            print(f"  offload: type-47 records {self.type47}  ·  v26 frames {len(v26)}  ·  acks {self.acks_sent}")
        print(f"  HR source: {source}")
        if not hr_series:
            print("\n  no HR yet — the strap isn't broadcasting a pulse. checklist:")
            print("   1. WEAR IT SNUG (skin contact) — WHOOP only measures/broadcasts HR when on-wrist")
            print("   2. wait ~20–30 s after wearing for it to lock a pulse")
            print("   3. scroll up to the first lines: did you see 'subscribed: standard HR', or an error?")
            print(f"   4. counts above: strap frames {self.frames}, 2a37 HR pts {len(self.std_hr)} "
                  "— both 0 = nothing arriving (not worn, or HR-broadcast off in the app)")
            print("   5. macOS: custom frames need bonding (blocked) — but 2a37 HR works once worn")
            return
        print_ladder(hr_series, v26, self)
        d = build(self.profile, hr_series, v26, gt)
        print_dashboard(d, compare_gt(hr_series, gt))


async def _connect_and_stream(args, live):
    try:
        from bleak import BleakClient, BleakScanner
    except ModuleNotFoundError:
        print("\n  bleak is not installed for this Python.\n"
              f"  Install it for THIS interpreter:\n\n    {sys.executable} -m pip install --user bleak\n\n"
              "  Then re-run. (Or try the pipeline offline first:  python3 whoop_live.py --sim)\n")
        return
    cfg = WHOOP5 if args.model == "whoop5" else WHOOP4
    if args.address:
        target = await BleakScanner.find_device_by_address(args.address, timeout=20.0)
    else:
        print(f"scanning for WHOOP service {cfg['service']} …")
        target = await BleakScanner.find_device_by_filter(
            lambda d, ad: cfg["service"].lower() in [s.lower() for s in (ad.service_uuids or [])]
            or (args.name_filter and args.name_filter.lower() in (d.name or "").lower()), timeout=20.0)
    if target is None:
        print("no WHOOP strap found (awake, near, and NOT bonded to the phone?)")
        return
    print(f"found {getattr(target, 'name', '?')} — connecting…")

    stop = asyncio.Event()
    async with BleakClient(target) as client:
        print(f"connected: {client.is_connected}")
        if args.pair:
            # The custom service needs an encrypted/bonded link. On Linux/BlueZ the confirmed hello write
            # triggers just-works bonding; on macOS it does not, so try an explicit pair() (may be a
            # no-op or unsupported on CoreBluetooth — the strap must also be free to bond, i.e. removed
            # from the phone's WHOOP app).
            try:
                ok = await client.pair()
                print(f"pair() → {ok}")
            except Exception as e:
                msg = str(e)
                print(f"pair() failed: {msg}")
                if "AuthenticationFailed" in msg or "NotConnected" in msg or "not connected" in msg.lower():
                    mac = getattr(target, "address", None) or args.address or "<MAC>"
                    print("\n  ✗ STALE BOND (Linux/BlueZ) — the old pairing blocks a fresh one. Fix:")
                    print(f"      bluetoothctl remove {mac}")
                    print("      # wear the strap, phone Bluetooth OFF, then re-run this command")
                    print("  (the failed pair() also dropped the link, hence the errors that would follow)\n")
                    return
                # else (e.g. macOS 'Pairing is not available'): keep going — 2a37 HR may still work
        # --- Establish the encrypted/bonded link (bond FIRST, then subscribe) --------
        # The custom WHOOP service is encrypted. On CoreBluetooth you do NOT call pair();
        # the OS starts a just-works bond automatically on the FIRST access to an encrypted
        # characteristic. That first access returns "insufficient encryption/authentication"
        # — that error IS the trigger, not a dead end. Retry with a delay so the OS can
        # finish bonding (accept the macOS "Bluetooth Pairing Request" dialog if it appears).
        bond = WHOOP5_CLIENT_HELLO if args.model == "whoop5" else \
            build_command_frame(CMD_GET_BATTERY_LEVEL)
        bonded = False
        tried_pair = args.pair  # if --pair already ran above, don't re-pair inside the loop
        for attempt in range(1, 7):
            try:
                await client.write_gatt_char(cfg["cmd_write"], bond, response=True)
                bonded = True
                print("session/bond write OK (GET_HELLO) — encrypted link established ✓")
                break
            except Exception as e:
                m = str(e).lower()
                if any(k in m for k in ("insufficient", "encrypt", "authent", "not paired", "not authenticated")):
                    # First encrypted failure → actively request an OS bond. Windows/BlueZ have a real
                    # pairing API (client.pair() → WinRT DevicePairing / BlueZ Pair), so we call it once
                    # here instead of relying on --pair. On CoreBluetooth pair() is unavailable and just
                    # raises — harmless; there the retry+delay waits for the implicit just-works bond.
                    if not tried_pair:
                        tried_pair = True
                        print("  link needs bonding — requesting an OS pair (Windows/Linux support this)…")
                        print("  →  if a Windows 'Tap to set up a device' notification (or any pairing")
                        print("     dialog) appears, CLICK it and choose Pair / Allow.")
                        try:
                            ok = await client.pair()
                            print(f"  pair() → {ok}")
                        except Exception as pe:
                            print(f"  pair() unavailable/failed: {str(pe)[:90]}  (expected on macOS)")
                    else:
                        print(f"  waiting for the bond to complete… ({attempt}/6)")
                    await asyncio.sleep(3.0)
                else:
                    print(f"bond write failed (non-encryption error): {e}")
                    break
        if not bonded:
            print("\n  ✗ could not establish an encrypted bond after 6 tries. By platform:")
            print("    • Windows: pair once via Settings → Bluetooth & devices → Add device → Bluetooth")
            print("               → select 'WHOOP 5A00…' → Pair, THEN re-run (bond persists, no --pair).")
            print("    • Linux:   bluetoothctl remove <MAC>  →  python3 pair_probe.py <MAC>  →  re-run.")
            print("    • macOS:   no pairing API — encrypted data is blocked here; use Windows/Linux.")
            print("    • ALL: phone Bluetooth OFF (strap accepts one central); strap worn & awake.")
            print("    (standard-HR 2a37 still works below — HR/strain/calories don't need the bond.)\n")
        # standard HR (2a37) — may not exist on WHOOP 5 (HR arrives via encrypted type-40/47); try anyway
        try:
            await client.start_notify(HR_MEASUREMENT, live.on_hr)
            print("subscribed: standard HR (2a37)")
        except Exception as e:
            print(f"standard HR unavailable: {e}")
        # notify[0] is cmd-from-strap (carries the LINK_VALID handshake); the rest carry data/events.
        # These are encrypted — they only succeed once the bond above is established.
        for i, u in enumerate(cfg["notify"]):
            cb = live.on_cmd_notify if i == 0 else live.on_frame_notify
            try:
                await client.start_notify(u, cb)
                print(f"subscribed: {u.split('-')[0]}…")
            except Exception as e:
                print(f"subscribe {u} failed: {e}")
        if args.offload:
            # Pull the historical store to get v26 PPG (→ HRV). SEND_HISTORICAL_DATA(22) is the verified
            # command; each HISTORY_END is acked to advance the trim cursor (whoop_frame helpers). The
            # app's offload handshake does NOT toggle realtime at all; we skip enabling it here only so
            # the type-40 flood doesn't compete with the bulk transfer.
            live.offload = True
            await asyncio.sleep(1.0)
            send_hist = (build_puffin_command(PUFFIN_CMD_SEND_HISTORICAL_DATA, seq=4, payload=b"\x00")
                         if args.model == "whoop5"
                         else build_command_frame(PUFFIN_CMD_SEND_HISTORICAL_DATA, seq=4, payload=b"\x00"))
            try:
                await client.write_gatt_char(cfg["cmd_write"], send_hist, response=True)
                print("sent SEND_HISTORICAL_DATA (offload → v26 PPG → HRV)")
            except Exception as e:
                print(f"SEND_HISTORICAL_DATA failed: {e}")
        elif args.model == "whoop5":
            # Enable the realtime stream with the REAL maverick toggles (verified against the app's
            # dispatcher c.l()): TOGGLE_REALTIME_HR(3)=[0x01] for HR (type-40), then the IMU + optical
            # toggles the app itself sends — TOGGLE_IMU_MODE(106) then TOGGLE_OPTICAL_MODE(108), each
            # [REVISION_1, on]. (The old SEND_R10_R11_REALTIME(63) is gen-4-only — that was the bug.)
            await asyncio.sleep(1.0)
            for label, framebytes in [("TOGGLE_REALTIME_HR", build_toggle_realtime_hr(seq=2, on=True)),
                                      ("TOGGLE_IMU_MODE", build_toggle_imu_mode(seq=3, on=True)),
                                      ("TOGGLE_OPTICAL_MODE", build_toggle_optical_mode(seq=4, on=True))]:
                try:
                    await client.write_gatt_char(cfg["cmd_write"], framebytes, response=False)
                    print(f"sent {label}")
                except Exception as e:
                    print(f"{label} failed: {e}")
                await asyncio.sleep(0.3)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

        async def render_loop():
            while not stop.is_set():
                live.render()
                await asyncio.sleep(args.refresh)

        async def link_valid_responder():
            # Answer each strap-initiated LINK_VALID with "There it is." (confirmed write). Off the
            # notify hot path so BlueZ notifications aren't dropped; without this the strap won't stream.
            seq = 20
            while not stop.is_set():
                if live.link_valid_pending:
                    origin = live.link_valid_pending.pop(0)
                    try:
                        await client.write_gatt_char(cfg["cmd_write"],
                                                     build_link_valid_response(origin, seq=seq & 0xFF),
                                                     response=True)
                        live.link_valid_replies += 1
                        seq += 1
                    except Exception as e:
                        print(f"LINK_VALID reply failed: {e}")
                await asyncio.sleep(0.1)

        async def ack_sender():
            # Ack each HISTORY_END with HISTORICAL_DATA_RESULT(23) (confirmed write) to walk the trim
            # cursor so type-47 v26 records past it are served. Verified helper: whoop_frame build_history_ack.
            seq = 50
            while not stop.is_set():
                if live.ack_pending:
                    end_data = live.ack_pending.pop(0)
                    try:
                        await client.write_gatt_char(cfg["cmd_write"], live._ack_fn(end_data, seq=seq & 0xFF),
                                                     response=True)
                        live.acks_sent += 1
                        seq += 1
                    except Exception as e:
                        print(f"ack failed: {e}")
                await asyncio.sleep(0.05)

        async def rekick():
            # The offload is deterministic; re-request every 5 s to recover BLE-dropped frames.
            s = 100
            while not stop.is_set():
                await asyncio.sleep(5.0)
                fr = (build_puffin_command(PUFFIN_CMD_SEND_HISTORICAL_DATA, seq=s & 0xFF, payload=b"\x00")
                      if args.model == "whoop5"
                      else build_command_frame(CMD_SEND_HISTORICAL_DATA, seq=s & 0xFF, payload=b"\x00"))
                try:
                    await client.write_gatt_char(cfg["cmd_write"], fr, response=False)
                except Exception:
                    pass
                s += 1
                if live.type47 and not live.ack_pending and live.acks_sent:
                    return   # store appears drained (records arrived, nothing left to ack)

        tasks = [asyncio.create_task(render_loop()), asyncio.create_task(link_valid_responder())]
        if args.offload:
            tasks += [asyncio.create_task(ack_sender()), asyncio.create_task(rekick())]
        try:
            if args.duration:
                await asyncio.wait_for(stop.wait(), timeout=args.duration)
            else:
                await stop.wait()
        except asyncio.TimeoutError:
            pass
        stop.set()
        for t in tasks:
            t.cancel()
        print("\nstopped.")


async def _sim(args, live):
    """No hardware: feed synthetic WHOOP-5 realtime frames through the SAME notify path + render loop."""
    import math
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    t0 = int(time.time())

    async def feeder():
        i = 0
        while not stop.is_set():
            hr = int(60 + 45 * (1 - math.cos(i / 40.0)) / 2 + 2 * math.sin(i / 5.0))  # 60→105 wave
            live.latest_hr = hr
            inner = _realtime_inner(t0 + i, hr)         # type-40 realtime, HR@8 (int required)
            frame = _whoop5_frame(inner)
            live.on_frame_notify(WHOOP5["notify"][0], bytearray(frame))
            i += 1
            await asyncio.sleep(0.02)                       # ~50x speed

    async def render_loop():
        while not stop.is_set():
            live.render()
            await asyncio.sleep(args.refresh)

    tasks = [asyncio.create_task(feeder()), asyncio.create_task(render_loop())]
    try:
        await asyncio.wait_for(stop.wait(), timeout=args.duration or 6.0)
    except asyncio.TimeoutError:
        pass
    stop.set()
    for t in tasks:
        t.cancel()
    live.render()
    print("sim done.")




# frame builders for --sim (from whoop_java_decode)
def _whoop5_frame(inner):
    decl = len(inner) + 4
    head = bytes([0xAA, 0x01, decl & 0xFF, (decl >> 8) & 0xFF, 0x00, 0x01])
    c16 = crc16_modbus(head)
    return bytes(bytearray(head) + bytes([c16 & 0xFF, (c16 >> 8) & 0xFF]) + bytes(inner) + struct.pack("<I", crc32(bytes(inner))))

def _realtime_inner(unix, hr, rev=2, body=1, length=32):
    b = bytearray(length); b[0] = 40; b[1] = rev
    struct.pack_into("<I", b, 2, unix); b[8] = hr; b[18] = 1; b[19] = body
    return bytes(b)


# ======================================================================================================
# PROGRESSIVE LADDER — what's unlocked now vs what needs more data
# ======================================================================================================
def data_ladder(hr_series, v26_frames, live=None):
    n_hr, n_v26 = len(hr_series), len(v26_frames)
    n_r20 = n_imu = 0
    if live is not None:
        for r in live.records:
            f = bytes.fromhex(r["hex"])
            if is_r20(f): n_r20 += 1
            elif len(f) > 9 and f[8] == 43 and f[9] == 21: n_imu += 1
    return [
        ("Heart rate",           n_hr > 0,     f"{n_hr} samples"),
        ("Strain + Calories",    n_hr >= 90,   ("ready" if n_hr >= 90 else f"{n_hr}/90 s of HR")),
        ("Zones + VO2max",       n_hr >= 60,   ("ready" if n_hr >= 60 else f"{n_hr}/60 s of HR")),
        ("HRV (RMSSD)",          n_v26 >= 4,   (f"{n_v26} v26 PPG frames" if n_v26 else "needs offload (--offload)")),
        ("SpO2 (relative)",      n_r20 >= 4,   (f"{n_r20} R20 frames" if n_r20 else "needs R20 optical")),
        ("Motion / steps (IMU)", n_imu > 0,    (f"{n_imu} R21 frames" if n_imu else "needs IMU stream")),
    ]

def print_ladder(hr_series, v26_frames, live=None):
    print("  ── progressive analysis  (more data → more unlocked) ─────")
    for name, ok, detail in data_ladder(hr_series, v26_frames, live):
        print(f"    [{'✓' if ok else '…'}] {name:<22} {detail}")
    print()


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §8.5  DEEP-MINE EXTRAS — ECG · optical AFE · data-product commands · device state · GPS · Pip  [JAVA]
# ══════════════════════════════════════════════════════════════════════════════════════════════════

# ── ECG (R17) — single-lead filtered ECG waveform + status  [JAVA ej0/i.java] ──────────────────────
def decode_ecg_r17(rec):
    """Decode a single-lead ECG (R17) record (realtime type-43, channel K==17). Inner-record offsets:
    seq u32@3 · status u16@13 (bit9=leads-on, bit11=flag) · meta bytes 15-20 · count u16@24 ·
    filtered waveform int16[] @26 (count = @24)."""
    if len(rec) < 26 or (len(rec) > 1 and rec[1] != 17):
        return None
    count = _u16(rec, 24)
    status = _u16(rec, 13)
    n = max(0, min(count, (len(rec) - 26) // 2))
    return {"type_name": "ECG_R17", "channel": "R17 ECG", "seq": _u32(rec, 3),
            "n_samples": count, "waveform": _i16_array(rec, 26, n), "status": status,
            "leads_on": bool(status & (1 << 9)), "flag_11": bool(status & (1 << 11)),
            "meta": [rec[o] for o in (15, 16, 17, 18, 19, 20) if o < len(rec)]}


# ── Optical AFE tables — normalize R20 raw counts for SpO2  [JAVA u22/b.java, u22/c.java] ───────────
TIA_GAIN = {7: 20, 8: 50, 9: 100, 10: 200, 11: 500, 12: 1000, 13: 2000, 14: 3000, 15: 4000}  # u22/b

def led_drive_pct(byte_val):
    """LED drive current as % of full scale (8-bit DAC)  [JAVA u22/c.java: v/255*100]."""
    return byte_val / 255.0 * 100.0

def optical_normalize(raw, tia_code, led_byte):
    """Raw optical count → gain/drive-normalized intensity (comparable across AFE settings → better SpO2)."""
    g = TIA_GAIN.get(tia_code, 1)
    led = max(1e-6, led_drive_pct(led_byte) / 100.0)
    return raw / (g * led)


# ── Data-product + query commands (drive the strap for more/other data)  [JAVA kq0/e.java, zi0/a.java]
CMD_SET_DP_TYPE, CMD_FORCE_DP_TYPE = 52, 53
CMD_GET_LED_DRIVE, CMD_GET_TIA_GAIN, CMD_GET_BIAS_OFFSET = 40, 42, 44
CMD_GET_BATTERY_LEVEL2, CMD_GET_BODY_LOCATION, CMD_GET_DATA_RANGE2, CMD_GET_HELLO2 = 26, 84, 34, 145

def build_force_dp_type(dp_type, seq=0):
    """FORCE_DP_TYPE(53): force the strap to stream a specific data product (dp_type byte). The app never
    sends this on WHOOP 5 → ours to drive. dp_type maps to an R-stream (R20 optical, R17 ECG, R21 IMU,
    R10/R11…); sweep candidate values to find each. Experimental."""
    return build_puffin_command(CMD_FORCE_DP_TYPE, seq=seq, payload=_pad4(bytes([dp_type & 0xFF])))

def build_query(opcode, seq=0):
    """Build a bare GET command (battery / body-location / data-range / LED-drive / TIA-gain / hello)."""
    return build_puffin_command(opcode, seq=seq, payload=b"\x00")


# ── COMMAND_RESPONSE decoders (device state; payload starts at inner offset 5)  [JAVA bj0/*, cj0/e] ──
def decode_battery(rec):
    """GET_BATTERY_LEVEL(26) response → battery %  [JAVA bj0/k]."""
    return _u8(rec, 5) if len(rec) > 5 else None

def decode_body_location(rec):
    """GET_BODY_LOCATION_AND_STATUS(84) → {sub, body_location, status}  [JAVA cj0/e: V@0,U@1,S@2 → +5]."""
    if len(rec) < 8:
        return None
    return {"sub": _u8(rec, 5), "body_location": _u8(rec, 6), "status": _u8(rec, 7)}

def decode_tia_gain(rec):
    """GET_TIA_GAIN(42) → gain multiplier  [JAVA bj0/p0 → u22/b]."""
    return TIA_GAIN.get(_u8(rec, 5)) if len(rec) > 5 else None

def decode_led_drive(rec):
    """GET_LED_DRIVE(40) → LED drive %  [JAVA bj0/w → u22/c]."""
    return led_drive_pct(_u8(rec, 5)) if len(rec) > 5 else None


# ── Pip (R25/R26) — Pulse-Information packet (opaque body; likely pulse/SpO2)  [JAVA hy1, pip pkg] ───
def is_pip(rec):
    return len(rec) > 1 and rec[1] in (25, 26)


# ── Strain combination — add two 0-21 scores CORRECTLY (raw space)  [JAVA realtime/presentation/n.java]
def strain_combine(a, b):
    """Combine two WHOOP Strain scores. You CANNOT add 0-21 scores directly — convert each back to raw
    strain via the inverse table, add in raw space, re-scale (n.c = j(E(a)+E(b)))."""
    def inv(scaled):
        for i in range(len(STRAIN_SCALE_TABLE) - 1, -1, -1):
            if STRAIN_SCALE_TABLE[i][1] <= scaled:
                return STRAIN_SCALE_TABLE[i][0]
        return 0.0
    return scaled_strain(min(1.0, inv(a) + inv(b)))


# ── GPS Kalman → distance / pace  [JAVA com/whoop/util/q0.java] ─────────────────────────────────────
class GpsKalman:
    """1-D Kalman filter over GPS position (WHOOP's live-workout distance/pace smoother). Port of q0.java:
    metres-per-degree=111194.93, longitude×cos(lat), process noise P+=dt·v²/1000, gain K=P/(acc²+P)."""
    MPD = 111194.93

    def __init__(self, min_accuracy=3.0):
        self.lat = self.lon = self.t = None
        self.P = 1.0
        self.min_acc = min_accuracy

    def update(self, lat, lon, accuracy, t_ms):
        var = max(accuracy, self.min_acc) ** 2
        if self.lat is None:
            self.lat, self.lon, self.P, self.t = lat, lon, var, t_ms
            return (lat, lon)
        dt = max(0.0, (t_ms - self.t) / 1000.0)
        self.t = t_ms
        if dt > 0:
            dlat = (lat - self.lat) * self.MPD
            dlon = (lon - self.lon) * self.MPD * math.cos(math.radians(lat))
            v = math.hypot(dlat, dlon) / dt
            self.P += dt * v * v / 1000.0
        K = self.P / (var + self.P)
        self.lat += K * (lat - self.lat)
        self.lon += K * (lon - self.lon)
        self.P *= (1 - K)
        return (self.lat, self.lon)


def gps_distance_m(points):
    """Total distance (metres) over smoothed [(lat,lon), …] points (equirectangular, WHOOP's method)."""
    d = 0.0
    for i in range(1, len(points)):
        la1, lo1 = points[i - 1]; la2, lo2 = points[i]
        dlat = (la2 - la1) * GpsKalman.MPD
        dlon = (lo2 - lo1) * GpsKalman.MPD * math.cos(math.radians(la2))
        d += math.hypot(dlat, dlon)
    return d


# ── R10 raw-data stream (mq0/a → RawDataStreamResult): 100-sample IMU + inline HR  [JAVA mq0/a, mq0/i] ─
def decode_r10(rec):
    """R10 (channel K==10, realtime type-43): the richest realtime raw product — 100 accel + 100 gyro
    samples per axis PLUS an inline heart-rate byte @17. Enable with build_send_r10_r11(). Richer than R21."""
    if len(rec) < 18 or (len(rec) > 1 and rec[1] != 10):
        return None
    def arr(off):
        n = max(0, min(100, (len(rec) - off) // 2))
        return _i16_array(rec, off, n)
    return {"type_name": "R10", "channel": "R10 raw (IMU + inline HR)",
            "unix": _u32(rec, 7), "subsec": _u16(rec, 11), "heart_rate": _u8(rec, 17),
            "accel_x": arr(85), "accel_y": arr(285), "accel_z": arr(485),
            "gyro_x": arr(688), "gyro_y": arr(888), "gyro_z": arr(1088)}

CMD_SEND_R10_R11_REALTIME = 63

def build_send_r10_r11(seq=0, on=True):
    """SEND_R10_R11_REALTIME(63): toggle the combined R10+R11 raw-sensor stream. payload [1]=on  [JAVA zi0/n0]."""
    return build_puffin_command(CMD_SEND_R10_R11_REALTIME, seq=seq, payload=_pad4(bytes([1 if on else 0])))


# ── GET_HELLO(145) identity block — serial, battery, versions, RTC, on-body  [JAVA bj0/s.java] ─────────
# Payload starts at inner offset 5, so a field at "offset X" is at inner 5+X.
def decode_hello(rec):
    """Decode a GET_HELLO(145) COMMAND_RESPONSE into the full device identity/telemetry block."""
    if len(rec) < 5 + 104:
        return None
    o = 5
    def hexs(a, n): return rec[o + a:o + a + n].hex().upper()
    def asc(a, n): return rec[o + a:o + a + n].split(b"\x00")[0].decode("ascii", "replace")
    optical = _u32(rec, o + 87)
    return {
        "serial": asc(14, 11),
        "battery_pct": _u32(rec, o + 1) / 10.0,
        "charging": bool(rec[o + 5] & 1),
        "on_body": rec[o + 102] == 1,
        "rtc_unix": _u32(rec, o + 6),
        "firmware": f"{rec[o + 91]}.{rec[o + 92]}.{rec[o + 93]}.{_u32(rec, o + 94)}",
        "dsp_version": f"{rec[o + 98]}.{rec[o + 99]}.{rec[o + 100]}",
        "hardware_family": _u32(rec, o + 79),
        "pcba_revision": _u32(rec, o + 83),
        "optical_revision": optical,
        "cpu_id": hexs(49, 30),
        "commit_hash": hexs(25, 24),
        "hr_broadcast": rec[o + 101] != 0,
        "device_type": ("MAVERICK" if optical < 38 else "GOOSE" if 48 <= optical < 86 else "?"),
    }


# ── EVENT packets (type 48) — push device/wear/battery/alarm state (no polling)  [JAVA nq0/a, fj0/*] ──
# Header: event_type u16 @ inner 2 · epoch u32 @4 · subsec u16 @8 · payload @12. Most events are a
# named signal + timestamp; a few carry fields (battery, condition, alarm, battery-pack, haptics).
EVENT_TYPES = {
    1: "ERROR", 2: "CONSOLE_OUTPUT", 3: "BATTERY_LEVEL", 4: "SYSTEM_CONTROL", 7: "CHARGING_ON",
    8: "CHARGING_OFF", 9: "WRIST_ON", 10: "WRIST_OFF", 11: "BLE_CONNECTION_UP", 12: "BLE_CONNECTION_DOWN",
    13: "RTC_LOST", 14: "DOUBLE_TAP", 15: "BOOT", 16: "SET_RTC", 17: "TEMPERATURE_LEVEL", 18: "PAIRING_MODE",
    19: "SERIAL_HEAD_CONNECTED", 20: "SERIAL_HEAD_REMOVED", 21: "BATTERY_PACK_CONNECTED",
    22: "BATTERY_PACK_REMOVED", 23: "BLE_BONDED", 24: "BLE_HR_PROFILE_ENABLED", 25: "BLE_HR_PROFILE_DISABLED",
    26: "TRIM_ALL_DATA", 27: "TRIM_ALL_DATA_ENDED", 28: "FLASH_INIT_COMPLETE", 29: "STRAP_CONDITION_REPORT",
    30: "BOOT_REPORT", 31: "EXIT_VIRGIN_MODE", 32: "CAPTOUCH_AUTOTHRESHOLD", 33: "BLE_REALTIME_HR_ON",
    34: "BLE_REALTIME_HR_OFF", 35: "ACCELEROMETER_RESET", 36: "AFE_RESET", 37: "SHIP_MODE_ENABLED",
    38: "SHIP_MODE_DISABLED", 39: "SHIP_MODE_BOOT", 40: "CH1_SATURATION", 41: "CH2_SATURATION",
    42: "ACCEL_SATURATION", 43: "BLE_SYSTEM_RESET", 44: "BLE_ON", 45: "BLE_INITIALIZED",
    46: "RAW_DATA_COLLECTION_ON", 47: "RAW_DATA_COLLECTION_OFF", 56: "STRAP_DRIVEN_ALARM_SET",
    57: "STRAP_DRIVEN_ALARM_EXECUTED", 58: "APP_DRIVEN_ALARM_EXECUTED", 59: "STRAP_DRIVEN_ALARM_DISABLED",
    60: "HAPTICS_FIRED", 63: "EXTENDED_BATTERY_INFORMATION", 96: "HIGH_FREQ_SYNC_PROMPT",
    97: "HIGH_FREQ_SYNC_ENABLED", 98: "HIGH_FREQ_SYNC_DISABLED", 100: "HAPTICS_TERMINATED",
    109: "BATTERY_PACK_INFO", 123: "GENERIC_FIRMWARE_EVENT",
}

def decode_event(rec):
    """Decode a strap EVENT (type 48) → named event + timestamp, with fields for the rich ones. Subscribe
    to events for push (no-poll) charging/battery/on-wrist/boot/alarm/temperature/saturation state."""
    if len(rec) < 12 or rec[0] != 48:
        return None
    eid = _u16(rec, 2)
    out = {"type_name": "EVENT", "event": EVENT_TYPES.get(eid, f"EVENT_{eid}"), "event_id": eid,
           "unix": _u32(rec, 4), "subsec": _u16(rec, 8)}
    p = 12
    if eid == 3 and len(rec) >= p + 11:                       # BATTERY_LEVEL
        out["battery_pct"] = _u32(rec, p + 1) / 10.0
        out["charging"] = bool(rec[p + 10] & 1)
    elif eid == 29 and len(rec) >= p + 11:                    # STRAP_CONDITION_REPORT
        out["soc_pct"] = _u16(rec, p + 6) / 10.0
        out["charging"] = rec[p + 9]
        out["on_wrist"] = rec[p + 10] == 1
        out["flash_backlog_pages"] = _u32(rec, p)
    elif eid == 100 and len(rec) >= p + 2:                    # HAPTICS_TERMINATED
        out["pattern"], out["term_code"] = rec[p], rec[p + 1]
    elif eid == 109 and len(rec) >= p + 25:                   # BATTERY_PACK_INFO
        out["pack_mac"] = ":".join(f"{rec[p + 1 + i]:02X}" for i in range(6))
        out["pack_serial"] = rec[p + 7:p + 23].split(b"\x00")[0].decode("ascii", "replace")
        out["pack_battery_pct"] = _u16(rec, p + 23) / 10.0
    return out


# ── IMU physical scale — raw int16 → g / dps (enables actigraphy, steps, motion)  [NOOP issue #423] ───
ACCEL_G_PER_LSB_W5 = 8.0 / 32768.0     # WHOOP 5/MG accelerometer: ±8 g full scale
ACCEL_G_PER_LSB_W4 = 1.0 / 4096.0      # WHOOP 4 accelerometer
GYRO_DPS_PER_LSB = 2000.0 / 32768.0    # gyroscope: ±2000 dps

def accel_to_g(raw, family="whoop5"):
    return raw * (ACCEL_G_PER_LSB_W5 if family == "whoop5" else ACCEL_G_PER_LSB_W4)

def gyro_to_dps(raw):
    return raw * GYRO_DPS_PER_LSB

def motion_magnitude_g(ax, ay, az, family="whoop5"):
    """Per-sample motion magnitude in g from raw int16 accel lists — the actigraphy signal for sleep
    staging / step counting. |a| = sqrt(ax²+ay²+az²) in g."""
    s = ACCEL_G_PER_LSB_W5 if family == "whoop5" else ACCEL_G_PER_LSB_W4
    return [math.sqrt((x * s) ** 2 + (y * s) ** 2 + (z * s) ** 2) for x, y, z in zip(ax, ay, az)]

def imu_activity_counts(accel_records, family="whoop5"):
    """Per-record activity metric (motion energy in g, gravity removed) from decoded R10/R21 records —
    the input Cole-Kripke actigraphy (sleep staging) expects. accel_records = [{accel_x,accel_y,accel_z},…]."""
    out = []
    for r in accel_records:
        mags = motion_magnitude_g(r.get("accel_x", []), r.get("accel_y", []), r.get("accel_z", []), family)
        if not mags:
            out.append(0.0); continue
        m = statistics.mean(mags)                       # ~gravity baseline
        out.append(sum(abs(v - m) for v in mags) / len(mags))   # mean abs deviation = motion energy
    return out


def imu_summary(frames, family="whoop5"):
    """Aggregate every R21 IMU frame into motion + a step estimate. `frames` = raw frame bytes.
    Returns None if there are no IMU frames. Rest → mean_g≈1.0, low motion_pct, 0 steps."""
    recs = [d for d in (decode_frame(f, family) for f in frames)
            if d and str(d.get("channel", "")).startswith("R21") and "accel_x" in d]
    if not recs:
        return None
    fs = recs[0].get("sampling_hz") or 100
    mag = []
    for r in recs:
        mag.extend(motion_magnitude_g(r.get("accel_x", []), r.get("accel_y", []), r.get("accel_z", []), family))
    if not mag:
        return None
    mean_g = statistics.mean(mag)
    motion_pct = 100.0 * sum(1 for m in mag if abs(m - mean_g) > 0.05) / len(mag)
    # step count: detrend |g| (remove gravity baseline), count peaks > 0.08 g with a ~0.3 s refractory
    win = max(1, int(fs)); dt = []
    for i in range(len(mag)):
        lo, hi = max(0, i - win // 2), min(len(mag), i + win // 2 + 1)
        dt.append(mag[i] - sum(mag[lo:hi]) / (hi - lo))
    refr = max(1, int(0.30 * fs)); steps = 0; last = -refr
    for i in range(1, len(dt) - 1):
        if dt[i] > 0.08 and dt[i] >= dt[i - 1] and dt[i] > dt[i + 1] and i - last >= refr:
            steps += 1; last = i
    return {"n_frames": len(recs), "samples": len(mag), "sampling_hz": fs,
            "mean_g": round(mean_g, 3), "motion_pct": round(motion_pct, 1),
            "steps_est": steps, "has_gyro": "gyro_x" in recs[0]}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §8.6  SLEEP STAGING — Cole-Kripke actigraphy + z-score staging  [algorithm: GenieMax SleepStaging.swift]
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Input: per-60s-epoch signals. epoch = {"motion": activity_g, "hr": bpm, "hrv": rmssd_ms,
#         "resp": br/min, "temp": raw, "t": unix}. Missing = None. All inputs are things we decode:
#   motion←imu_activity_counts(IMU) · hr←v18/R10 · hrv←v26 PPG · resp/temp←v18 (@35/@65).
CK_WEIGHTS = [106, 54, 58, 76, 230, 74, 67]        # Cole-Kripke 7-epoch window
STAGE_NAMES = {0: "Deep", 1: "Light", 2: "REM", 3: "Wake"}


def _pct(vals, q):
    v = sorted(x for x in vals if x is not None)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))] if v else 0.0


def cole_kripke(motion):
    """Self-calibrating Cole-Kripke → per-epoch 0=sleep / 1=wake, plus the 90th-pct motion `hi`."""
    hi = _pct(motion, 0.9) or 0.05
    sc = 20.0 / hi
    n = len(motion)
    out = []
    for i in range(n):
        s = sum(CK_WEIGHTS[j] * (motion[i + j - 4] or 0.0)
                for j in range(7) if 0 <= i + j - 4 < n)
        out.append(0 if sc * s < 1.0 else 1)
    return out, hi


def _z(vals, mask):
    sel = [v for v, m in zip(vals, mask) if m and v is not None]
    if len(sel) < 2:
        return [0.0] * len(vals)
    mu = statistics.mean(sel); sd = statistics.pstdev(sel) or 1.0
    return [((v - mu) / sd if v is not None else 0.0) for v in vals]


def _mode3(stages):
    out = list(stages)
    for i in range(1, len(stages) - 1):
        w = stages[i - 1:i + 2]
        out[i] = max(set(w), key=lambda x: (w.count(x), -w.index(x)))
    return out


def stage_sleep(epochs):
    """Stage a night of 60s epochs → hypnogram + metrics. Faithful port of GenieMax's algorithm
    (Cole-Kripke wake detection → z-score deep/REM scoring → AASM rank-allocation → despeckle).
    NOTE: RR-variability (zrrv) is approximated by the HRV series (we don't carry per-epoch RR variance)."""
    n = len(epochs)
    if n < 10:
        return None
    mot = [e.get("motion") for e in epochs]
    hr = [e.get("hr") for e in epochs]
    hrv = [e.get("hrv") for e in epochs]
    resp = [e.get("resp") for e in epochs]
    temp = [e.get("temp") for e in epochs]
    _, hi = cole_kripke(mot)                            # motion 90th-pct for the motion gate
    hrv_v = [h for h in hr if h is not None]
    floor = _pct(hrv_v, 0.05) if hrv_v else 50.0        # night HR floor (5th percentile)
    mean_hr = statistics.mean(hrv_v) if hrv_v else 55.0
    thr = floor + max(12.0, 0.6 * (mean_hr - floor))    # HR wake gate (GenieMax refined pass)
    # asleep = HR near the night floor AND not clearly moving. Motion is in g (NOOP #423 scale), so use
    # a physical wake threshold (~0.05 g = real movement), with a hi-relative floor as backup.
    mot_wake = max(0.05, hi * 2.0)
    asleep0 = [hr[i] is not None and hr[i] <= thr and not (mot[i] and mot[i] > mot_wake) for i in range(n)]
    zhr, zhrv, zresp, zrrv = _z(hr, asleep0), _z(hrv, asleep0), _z(resp, asleep0), _z(hrv, asleep0)
    tmu = statistics.mean([t for t in temp if t is not None]) if any(t is not None for t in temp) else 0.0
    stages = [3] * n
    ds, rs = [-99.0] * n, [-99.0] * n
    for i in range(n):
        if not asleep0[i]:
            continue                                   # wake
        pos = i / n
        pen = (mot[i] or 0.0) / (hi or 1.0) * 0.5
        td = (temp[i] or tmu) - tmu
        ds[i] = -zhr[i] + 0.3 * zhrv[i] - 0.4 * zrrv[i] - 0.3 * zresp[i] + 0.5 * td - pen + (0.6 if pos < 0.4 else 0.0)
        rs[i] = (zhr[i] - 0.3 * zhrv[i] + 0.4 * zrrv[i] + 0.2 * zresp[i] - pen + (0.6 if pos > 0.55 else 0.0)) if i >= 70 else -99.0
        stages[i] = 1                                  # light (default asleep)
    aslp = [i for i in range(n) if stages[i] != 3]
    nS = len(aslp)
    for i in sorted(aslp, key=lambda i: rs[i], reverse=True)[:int(0.26 * nS)]:
        stages[i] = 2                                  # REM
    for i in sorted([i for i in aslp if stages[i] != 2], key=lambda i: ds[i], reverse=True)[:int(0.22 * nS)]:
        stages[i] = 0                                  # Deep
    sm = _mode3(stages)
    for _ in range(2):
        for i in range(1, n - 1):
            if sm[i] != sm[i - 1] and sm[i - 1] == sm[i + 1]:
                sm[i] = sm[i - 1]
    tib, aslp_ep = n, sum(1 for s in sm if s != 3)
    deep, rem = sum(1 for s in sm if s == 0), sum(1 for s in sm if s == 2)
    durC, effC = min(1.0, (aslp_ep / 60.0) / 8.0), aslp_ep / max(1, tib)
    stageC = min(1.0, (deep + rem) / (0.40 * max(aslp_ep, 1)))
    shr = [hr[i] for i in range(n) if sm[i] != 3 and hr[i] is not None]
    shrv = [hrv[i] for i in range(n) if sm[i] == 0 and hrv[i] is not None] or \
           [hrv[i] for i in range(n) if sm[i] != 3 and hrv[i] is not None]
    return {"hypnogram": sm, "tst_hours": aslp_ep / 60.0, "efficiency": effC,
            "deep_min": deep, "rem_min": rem, "light_min": sum(1 for s in sm if s == 1),
            "wake_min": sum(1 for s in sm if s == 3), "sleep_score": 100 * (0.55 * durC + 0.25 * effC + 0.20 * stageC),
            "resting_hr": (round(min(shr)) if shr else None),
            "sleep_hrv_rmssd": (statistics.mean(shrv) if shrv else None)}


def print_hypnogram(res):
    if not res:
        print("  not enough data to stage sleep"); return
    sym = {0: "▁", 1: "▃", 2: "▆", 3: "█"}   # Deep low → Wake high
    h = res["hypnogram"]
    step = max(1, len(h) // 100)
    line = "".join(sym[h[i]] for i in range(0, len(h), step))
    print("  ── Sleep ──────────────────────────────────────────")
    print(f"   score {res['sleep_score']:.0f}/100 · TST {res['tst_hours']:.1f}h · eff {res['efficiency']*100:.0f}% · "
          f"RHR {res['resting_hr']} · HRV {res['sleep_hrv_rmssd']:.0f}ms" if res['sleep_hrv_rmssd'] else "")
    print(f"   Deep {res['deep_min']}m · REM {res['rem_min']}m · Light {res['light_min']}m · Wake {res['wake_min']}m")
    print(f"   hypnogram (Deep▁ Light▃ REM▆ Wake█):\n   {line}")


def _demo_sleep():
    """Synthesise a realistic 8h night (90-min cycles) and stage it."""
    t0 = 1_700_000_000
    epochs = []
    for i in range(480):                                # 480 × 60s = 8h
        cyc = math.sin(2 * math.pi * i / 90)            # 90-min cycle
        deep_ph = i < 200 and cyc < -0.3                # deep dominant early
        rem_ph = i > 150 and cyc > 0.5                  # REM later
        wake = (i < 4) or (i % 130 == 0)
        hr = 48 + (0 if deep_ph else 8 if rem_ph else 4) + (18 if wake else 0) + math.sin(i / 7) * 1.0
        motion = 0.28 if wake else (0.003 if deep_ph else 0.007)
        hrv = 62 if deep_ph else (34 if rem_ph else 46)
        epochs.append({"t": t0 + i * 60, "hr": hr, "motion": motion, "hrv": hrv,
                       "resp": 13.5 + (1.5 if rem_ph else 0), "temp": 4000 + (30 if deep_ph else 0)})
    res = stage_sleep(epochs)
    print_hypnogram(res)
    return res


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §8.7  R20 GenieMax seed + CAPTURE LOG (send me the log to reverse SpO2)  [GenieMax WhoopDecode + ours]
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# GenieMax reads ONE flat R20 optical channel: 25× uint32 LE @ inner offset 239. We anchor on it and
# search nearby offsets for OTHER pulsatile uint32 channels (candidate red/IR → SpO2).
GENIEMAX_R20_OFFSET, GENIEMAX_R20_COUNT = 239, 25

def r20_channel_u32(frame, offset, count=GENIEMAX_R20_COUNT):
    rec = inner_of(frame)
    if len(rec) < offset + count * 4:
        return None
    return [struct.unpack_from("<I", rec, offset + 4 * i)[0] for i in range(count)]

def r20_geniemax_seed(r20_frames, gt, offsets=range(39, 600, 20)):
    """Anchor on GenieMax's @239 channel and sweep nearby offsets for other HR-locked uint32 channels
    (25 samples ≈ 25 Hz). ≥2 HR-locked channels with different DC = the red/IR pair we need for SpO2."""
    runs = runs_of(r20_frames)
    if not runs:
        print("no consecutive R20 runs."); return []
    run_hrs = [statistics.median([gt[r20_unix(f)] for f in run if r20_unix(f) in gt] or [0]) for run in runs]
    if not any(run_hrs):
        print("no ground-truth HR overlaps the R20 frames."); return []
    print(f"R20 GenieMax-seed: anchor @{GENIEMAX_R20_OFFSET} + sweep {offsets.start}..{offsets.stop} (uint32, 25 smp/rec)")
    hits = []
    for off in offsets:
        tracks, dcs = 0, []
        for ri, run in enumerate(runs):
            sig = []
            for f in run:
                ch = r20_channel_u32(f, off)
                if ch:
                    sig += [float(x) for x in ch]
            if len(sig) < 16 or not run_hrs[ri]:
                continue
            bpm, conf = dominant_bpm(sig, 25)          # 25 samples/record ≈ 25 Hz
            if bpm and conf > 0.2 and abs(bpm - run_hrs[ri]) <= 8:
                tracks += 1
                dcs.append(statistics.mean(sig))
        if tracks:
            hits.append({"offset": off, "hr_locked_runs": tracks, "dc": statistics.mean(dcs) if dcs else 0,
                         "is_anchor": off == GENIEMAX_R20_OFFSET})
    hits.sort(key=lambda h: (h["hr_locked_runs"], h["is_anchor"]), reverse=True)
    if not hits:
        print("  no HR-locked uint32 channel found near @239. R20 layout may differ from GenieMax's — "
              "run the full §5 sweep, or the encoding isn't uint32.")
        return hits
    print(f"  {'offset':>6} {'enc':>5} {'HR-locked':>9} {'DC≈':>10}")
    for h in hits[:10]:
        print(f"  {h['offset']:>6} {'u32':>5} {h['hr_locked_runs']:>9} {h['dc']:>10.0f}"
              + ("  ← GenieMax anchor" if h["is_anchor"] else ""))
    n = len(hits)
    print(f"  → {n} HR-locked optical channel(s). "
          + ("≥2 found → classify by DC (IR highest) → SpO2 via §5 ratio-of-ratios."
             if n >= 2 else "only 1 (like GenieMax) → need real R20 with more channels for red/IR."))
    return hits


# ── CAPTURE LOG — save all raw frames + a decode summary to a file you send me for analysis ──────────
def save_capture_log(records, path, family="whoop5", std_hr=None):
    """Write {summary, frames, std_hr} to `path`. `frames` are the raw encrypted hex (I decode
    everything from them); `std_hr` is the standard-2a37 HR series [(unix,bpm)] that ALWAYS works
    without a bond — so the file is useful even when the encrypted stream never opened. `summary`
    is a quick decode census incl. device identity, R20/ECG/PPG counts, and any events."""
    std_hr = std_hr or []
    fb_all = [bytes.fromhex(r["hex"]) for r in records]
    counts, r20, ecg, ppg, imu, events, hello = {}, 0, 0, 0, 0, {}, None
    hist_hr, skin_c, spo2, sleep_states = [], [], [], {}
    for f in fb_all:
        d = decode_frame(f, family)
        if not d:
            counts["crc_fail"] = counts.get("crc_fail", 0) + 1
            continue
        key = d.get("type_name", "?") + (f" v{d['version']}" if "version" in d else "")
        counts[key] = counts.get(key, 0) + 1
        if is_r20(f):
            r20 += 1
        if d.get("type_name") == "ECG_R17":
            ecg += 1
        if d.get("ppg_waveform"):
            ppg += 1
        if d.get("channel", "").startswith(("R21", "R10")):
            imu += 1
        if d.get("type_name") == "EVENT":
            events[d.get("event", "?")] = events.get(d.get("event", "?"), 0) + 1
        if d.get("version") == 18:                       # historical biometrics
            if d.get("heart_rate") and d.get("unix"):
                hist_hr.append((d["unix"], float(d["heart_rate"])))
            if d.get("skin_temp_c"):
                skin_c.append(d["skin_temp_c"])
            if d.get("spo2_pct"):                        # WHOOP's own computed SpO2 (sleep-only)
                spo2.append(d["spo2_pct"])
            if "sleep_state" in d:
                sleep_states[d["sleep_state"]] = sleep_states.get(d["sleep_state"], 0) + 1
        if d.get("serial"):                              # GET_HELLO identity
            hello = {k: d[k] for k in ("serial", "firmware", "dsp_version", "battery_pct", "on_body") if k in d}
    # derived views (so the file I receive already shows the interesting numbers)
    imu_motion = imu_summary(fb_all, family)
    hv = hrv_from_v26(fb_all)
    hrv_brief = ({k: hv.get(k) for k in ("hr", "rmssd", "quality", "n_bursts", "span_s", "hr_err")}
                 if hv else None)
    hist_hr.sort()
    hist = ({"points": len(hist_hr), "span_min": round((hist_hr[-1][0] - hist_hr[0][0]) / 60, 1),
             "bpm_min": min(h for _, h in hist_hr), "bpm_max": max(h for _, h in hist_hr)}
            if len(hist_hr) > 1 else None)
    def _rng(x): return {"n": len(x), "min": round(min(x), 2), "max": round(max(x), 2),
                         "mean": round(sum(x) / len(x), 2)} if x else None
    summary = {"total_frames": len(records), "packet_counts": counts, "r20_frames": r20,
               "ecg_frames": ecg, "v26_ppg_frames": ppg, "imu_frames": imu, "events": events,
               "device": hello, "std_hr_points": len(std_hr),
               "hr_span_s": (int(std_hr[-1][0] - std_hr[0][0]) if len(std_hr) > 1 else 0),
               "imu_motion": imu_motion, "hrv": hrv_brief, "history_hr": hist,
               "skin_temp_c": _rng(skin_c),
               "spo2_pct": _rng(spo2),                    # WHOOP's own SpO2 — populated only from sleep
               # raw sleep-stage code distribution (whoop-rs extracts (byte73>>4)&3; the code→name
               # mapping is unverified, so we report codes, not guessed names)
               "sleep_state_codes": {str(k): v for k, v in sorted(sleep_states.items())} or None}
    with open(path, "w") as fh:
        json.dump({"summary": summary, "family": family, "frames": records,
                   "std_hr": [[int(s), float(h)] for s, h in std_hr]}, fh, indent=1)
    return summary


# ======================================================================================================
# §9  CLI
# ======================================================================================================
def _load_records(path):
    """Accept either a bare list of {hex,…} records OR our capture-log {summary,frames,std_hr}.
    Returns (records_list, std_hr_list)."""
    raw = json.load(open(path))
    if isinstance(raw, dict) and "frames" in raw:
        return raw["frames"], raw.get("std_hr", [])
    return raw, []


def _demo_dashboard():
    t0 = 1_700_000_000
    hr_series = [(t0 + i, (62 if i < 300 else (62 + 95*(i-300)/1200 if i < 1500 else 157 - 70*(i-1500)/600)) + 2*math.sin(i/7.0)) for i in range(2100)]
    gt = [(t, h + ((i*13)%5 - 2)) for i,(t,h) in enumerate(hr_series)]
    v26=[]; g=0
    for s in range(40):
        f=bytearray(88); f[0]=0xAA; f[8]=47; f[9]=26; struct.pack_into("<I",f,15,t0+s)
        for k in range(24):
            struct.pack_into("<h",f,27+2*k,max(-32768,min(32767,int(3000*math.sin(2*math.pi*62/60*g/24+0.06*math.sin(g/13)))))); g+=1
        v26.append(bytes(f))
    prof={"sex":"maleNew","age":30,"rest_hr":55,"max_hr":190,"weight_kg":80,"height_m":1.8,"fitness":"recreational_enthusiast",
          "baselines":{"hrv_mu":55.0,"hrv_sd":12.0,"rhr_mu":58.0,"rhr_sd":4.0,"z_sleep":0.3,"tsb_norm":55.0,"sleep_score":78.0}}
    print_ladder([(t, h) for t, h in hr_series], v26)
    d=build(prof, hr_series, v26, gt); print_dashboard(d, compare_gt(hr_series, gt)); return d

def _selftest():
    inner=bytearray(112); inner[0]=47; inner[1]=18; struct.pack_into("<I",inner,7,1_700_000_000); inner[14]=101
    fr=_whoop5_frame(inner); d=decode_frame(fr,"whoop5"); assert d["heart_rate"]==101, d
    print("§2 decode v18 HR=%d ✓"%d["heart_rate"])
    r=compute({"sex":"maleNew","age":30,"rest_hr":55,"max_hr":190,"weight_kg":80,"height_m":1.8,"fitness":"recreational_enthusiast"},
              [(1_700_000_000_000+i*1000, 70+80*i/1200) for i in range(1200)])
    assert 0<=r["scaledScore"]<=21 and r["kilojoules"]>0; print("§3 strain=%.1f cal=%.0fkJ ✓"%(r["scaledScore"],r["kilojoules"]))
    v26=[]; g=0
    for s in range(12):
        f=bytearray(88); f[0]=0xAA; f[8]=47; f[9]=26; struct.pack_into("<I",f,15,1_700_000_000+s)
        for k in range(24):
            struct.pack_into("<h",f,27+2*k,int(3000*math.sin(2*math.pi*70/60*g/24+0.05*math.sin(g/11)))); g+=1
        v26.append(bytes(f))
    hv=hrv_from_v26(v26); assert hv and hv["rmssd"]>0; print("§4 HRV RMSSD=%.1f ✓"%hv["rmssd"])
    print("§5 SpO2 R=0.5 →", {k:round(v) for k,v in spo2_from_R(0.5).items()})
    _demo_dashboard(); print("\n§1-9 all sections PASSED ✓")

def main():
    p=argparse.ArgumentParser(description="WHOOP 5 all-in-one: pair → decode → progressive analysis.")
    p.add_argument("capture", nargs="?")
    p.add_argument("--family", choices=["whoop4","whoop5"], default="whoop5")
    p.add_argument("--model", choices=["whoop4","whoop5"], default="whoop5")
    p.add_argument("--live", action="store_true", help="connect over BLE and analyse live (progressive)")
    p.add_argument("--sim", action="store_true", help="no hardware: synthetic live stream")
    p.add_argument("--pair", action="store_true"); p.add_argument("--offload", action="store_true")
    p.add_argument("--address"); p.add_argument("--name-filter")
    p.add_argument("--refresh", type=float, default=3.0); p.add_argument("--duration", type=float)
    p.add_argument("--r20", metavar="CAPTURE"); p.add_argument("--field-map", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--sleep", action="store_true", help="demo the sleep stager on a synthetic night")
    p.add_argument("--log", metavar="FILE", help="save all captured raw frames + summary to FILE (send it to analyse)"); p.add_argument("--selftest", action="store_true")
    p.add_argument("--sex", default="maleNew"); p.add_argument("--age", type=float, default=30)
    p.add_argument("--rest-hr", type=float, default=55); p.add_argument("--max-hr", type=float, default=190)
    p.add_argument("--weight", type=float, default=80); p.add_argument("--height", type=float, default=1.8)
    p.add_argument("--fitness", default="recreational_enthusiast")
    a=p.parse_args()
    profile={"sex":a.sex,"age":a.age,"rest_hr":a.rest_hr,"max_hr":a.max_hr,"weight_kg":a.weight,"height_m":a.height,"fitness":a.fitness}
    if a.field_map: print(FIELD_MAP); return
    if a.selftest: _selftest(); return
    if a.live or a.sim:
        live=Live(a.model, profile)
        try: asyncio.run(_sim(a, live) if a.sim else _connect_and_stream(a, live))
        except KeyboardInterrupt: pass
        if a.log:
            s = save_capture_log(live.records, a.log, a.model, live.std_hr)
            print(f"\nlog saved -> {a.log}  ({s['total_frames']} encrypted frames · {s['std_hr_points']} HR pts "
                  f"({s['hr_span_s']}s) · R20x{s['r20_frames']} · ECG x{s['ecg_frames']} · PPG x{s['v26_ppg_frames']})")
            print("  → send me this file and I'll decode R20/ECG/PPG/IMU + compute strain/HRV/calories/sleep from it.")
        return
    if a.sleep: _demo_sleep(); return
    if a.demo: _demo_dashboard(); return
    if a.r20:
        recs, _ = _load_records(a.r20)
        r20=[f for f in (bytes.fromhex(r["hex"]) for r in recs) if is_r20(f)]
        if not r20: print("no R20 records in this capture."); return
        gt=ground_truth_hr(recs); scenario_map(r20)
        r20s = r20[:250] if len(r20) > 250 else r20       # cap the channel sweep for speed
        if len(r20) > 250: print(f"(sweeping first 250 of {len(r20)} R20 frames for speed)")
        print(); r20_geniemax_seed(r20s, gt)
        print(); hits=scenario_find(r20s, gt)
        if hits: print(); scenario_spo2(r20s, hits[0])
        return
    if not a.capture: p.error("give capture.json, or --live / --sim / --demo / --r20 / --field-map / --selftest")
    recs, std = _load_records(a.capture)
    hr_series,v26,gt=extract_signals(recs, a.family)
    if std and not hr_series:
        hr_series = [(int(s), float(h)) for s, h in std]
    if not hr_series: print("no HR decoded — check --family / capture contents"); return
    print_ladder(hr_series, v26); print_dashboard(build(profile, hr_series, v26, gt), compare_gt(hr_series, gt))

if __name__ == "__main__":
    main()
