"""Present a Switch 2 controller to Linux as a Bluetooth Switch Pro Controller.

Why: Steam (SDL's HIDAPI Switch driver) and the kernel's hid-nintendo both know
the original Pro Controller protocol, including gyro and HD rumble. A plain
uinput gamepad cannot carry motion data into Steam Input. This module creates a
virtual HID device through /dev/uhid that speaks the original protocol and is
fed from the Switch 2 BLE reports.

Needs write access to /dev/uhid (see steamos/setup-admin.sh).

Protocol facts are taken from the kernel's hid-nintendo driver and SDL's
SDL_hidapi_switch.c / SDL_hidapi_switch2.c (axis conventions, scale factors).
"""

from __future__ import annotations

import logging
import math
import os
import struct
import threading
import time
from typing import Callable, Optional

from . import protocol as P

logger = logging.getLogger(__name__)

UHID_PATH = "/dev/uhid"

# uhid event types (include/uapi/linux/uhid.h)
UHID_DESTROY = 1
UHID_START = 2
UHID_STOP = 3
UHID_OPEN = 4
UHID_CLOSE = 5
UHID_OUTPUT = 6
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14
UHID_EVENT_SIZE = 4 + 4372
UHID_DATA_MAX = 4096

BUS_BLUETOOTH = 0x05
PROCON_VID = 0x057E
PROCON_PID = 0x2009

REPORT_LEN = 49          # report id + 48 bytes, as sent over Bluetooth
REPORT_PERIOD_S = 0.012  # hid-nintendo only trusts input deltas of 8..17 ms

# Emulated stick calibration: centre 2048, +/-1500 counts of travel.
STICK_CENTER = 2048
STICK_RANGE = 1500

# Emulated IMU calibration. The original protocol converts with
#   accel_g = raw * 4 / (coeff - origin)      gyro_dps = raw * 936 / (coeff - origin)
# Switch 2 raw samples are 4096 LSB/g and (gyro_coeff rad/s full scale) / 32767,
# so choosing the coefficients below lets raw samples pass through unscaled.
ACCEL_COEFF = 16384
GYRO_REF_COEFF = 34.8  # rad/s full scale that the emulated calibration describes


def gyro_cal_coeff(gyro_coeff_rad: float) -> int:
    dps_per_lsb = gyro_coeff_rad * 180.0 / math.pi / 32767.0
    return int(round(936.0 / dps_per_lsb))


def _report_descriptor() -> bytes:
    """Vendor-defined reports matching the Bluetooth Pro Controller layout."""
    d = bytearray()
    d += bytes([0x05, 0x01, 0x09, 0x05, 0xA1, 0x01])          # Generic Desktop / Gamepad, Application
    d += bytes([0x06, 0x01, 0xFF])                            # Usage Page (vendor 0xFF01)
    d += bytes([0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08])    # 0..255, 8-bit fields
    for rid in (0x21, 0x30, 0x31, 0x3F, 0x81):                # input reports
        d += bytes([0x85, rid, 0x09, rid, 0x95, REPORT_LEN - 1, 0x81, 0x02])
    for rid in (0x01, 0x10, 0x11, 0x12, 0x80):                # output reports
        d += bytes([0x85, rid, 0x09, rid, 0x95, REPORT_LEN - 1, 0x91, 0x02])
    d += bytes([0xC0])
    return bytes(d)


def pack12(a: int, b: int) -> bytes:
    """Two 12-bit values into three bytes (stick and calibration packing)."""
    a &= 0xFFF
    b &= 0xFFF
    return bytes([a & 0xFF, (a >> 8) | ((b & 0x0F) << 4), b >> 4])


def stick_raw(x: float, y: float) -> bytes:
    def one(v: float) -> int:
        v = max(-1.0, min(1.0, v))
        return int(round(STICK_CENTER + v * STICK_RANGE))
    return pack12(one(x), one(y))


def decode_amp(encoded: int) -> float:
    """Inverse of the HD-rumble amplitude encoding. ``encoded`` is the even
    0..200 high-band value (low band uses encoded/2 as its index)."""
    e = max(0, min(200, encoded))
    if e < 2:
        return 0.0
    if e < 0x20:
        return 0.010 * (11.7 ** ((e - 2) / 30.0))
    if e < 0x40:
        return min(1.0, (2.0 ** ((e + 188) / 32.0)) / 1000.0)
    return min(1.0, (2.0 ** (((e + 246) / 2.0 + 96) / 32.0)) / 1000.0)


def decode_rumble(data: bytes) -> tuple[float, float]:
    """Return (low_band, high_band) amplitude 0..1 from 8 bytes (left+right)."""
    low = high = 0.0
    for off in (0, 4):
        side = data[off:off + 4]
        if len(side) < 4:
            continue
        hf_enc = side[1] & 0xFE
        lf_enc = ((side[3] & 0x7F) - 0x40) * 2 + (side[2] >> 7) if side[3] >= 0x40 else 0
        high = max(high, decode_amp(hf_enc))
        low = max(low, decode_amp(lf_enc * 2))
    return low, high


def battery_nibble(mv: Optional[int]) -> int:
    """High nibble of the battery/connection byte: level (0,2,4,6,8), bit0 charging."""
    if not mv:
        return 8
    pct = (mv - 2950) * 100 / (4200 - 2950)
    if pct >= 75:
        return 8
    if pct >= 50:
        return 6
    if pct >= 25:
        return 4
    if pct >= 10:
        return 2
    return 0


def parse_extra_buttons(spec: str) -> list[tuple[int, int]]:
    """'GL=L_STK,GR=R_STK,C=CAPTURE' -> [(src_mask, dst_mask), ...]."""
    out = []
    for item in (spec or "").split(","):
        if "=" not in item:
            continue
        src, dst = (s.strip().upper() for s in item.split("=", 1))
        if src in P.SWITCH_BUTTONS and dst in P.SWITCH_BUTTONS:
            out.append((P.SWITCH_BUTTONS[src], P.SWITCH_BUTTONS[dst]))
        else:
            logger.warning("ignoring unknown button mapping %r", item)
    return out


def uhid_available() -> bool:
    return os.access(UHID_PATH, os.R_OK | os.W_OK)


class SpiFlash:
    """The few flash regions that hid-nintendo and SDL read."""

    def __init__(self, gyro_coeff_rad: float, colors: Optional[tuple] = None):
        self.regions: dict[int, bytes] = {}
        self.set_gyro_coeff(gyro_coeff_rad)
        travel = pack12(STICK_RANGE, STICK_RANGE)
        center = pack12(STICK_CENTER, STICK_CENTER)
        # Left: max-above, centre, min-below.  Right: centre, min-below, max-above.
        self.regions[0x603D] = travel + center + travel + center + travel + travel
        body = bytes(colors[0]) if colors and len(colors[0]) == 3 else b"\x32\x32\x32"
        buttons = bytes(colors[1]) if colors and len(colors[1]) == 3 else b"\xFF\xFF\xFF"
        self.regions[0x6050] = body + buttons + body + body

    def set_gyro_coeff(self, gyro_coeff_rad: float) -> None:
        g = gyro_cal_coeff(gyro_coeff_rad)
        self.regions[0x6020] = struct.pack("<12h", 0, 0, 0, ACCEL_COEFF, ACCEL_COEFF, ACCEL_COEFF,
                                           0, 0, 0, g, g, g)

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray(b"\xFF" * length)  # erased flash everywhere else
        for start, blob in self.regions.items():
            lo, hi = max(addr, start), min(addr + length, start + len(blob))
            if lo < hi:
                out[lo - addr:hi - addr] = blob[lo - start:hi - start]
        return bytes(out)


class ProconUHID:
    """Virtual Bluetooth Pro Controller. Same surface as gamepad.SwitchGamepad
    (update / release_all / close / rumble_cb) plus update_motion."""

    destroy_on_disconnect = True

    def __init__(self, name: str, mac: str, colors: Optional[tuple] = None,
                 extra_buttons: str = ""):
        self.mac = mac.upper()
        self.rumble_cb: Optional[Callable[[float, float], None]] = None
        self.led_cb: Optional[Callable[[int], None]] = None
        self.power_off_cb: Optional[Callable[[], None]] = None

        self.gyro_coeff = GYRO_REF_COEFF  # refined from sensor timestamps
        self.gyro_bias = (0.0, 0.0, 0.0)  # rad/s, Switch 2 raw axis order
        self.flash = SpiFlash(GYRO_REF_COEFF, colors)
        self._extra = parse_extra_buttons(extra_buttons)

        self._lock = threading.Lock()
        self._buttons = bytes(3)
        self._left = stick_raw(0.0, 0.0)
        self._right = stick_raw(0.0, 0.0)
        self._imu = struct.pack("<6h", 0, 0, 4096, 0, 0, 0)
        self._battery = 8
        self._timer = 0
        self._running = True
        self._started = threading.Event()
        self._last_rumble = (0.0, 0.0)
        self._last_rumble_at = 0.0

        # Sensor timestamp unit detection (decides the gyro full-scale).
        self._ts_first: Optional[tuple[float, int]] = None
        self._ts_samples = 0
        self._ts_done = False

        self._fd = os.open(UHID_PATH, os.O_RDWR | os.O_CLOEXEC)
        rd = _report_descriptor()
        create = struct.pack(
            "<I128s64s64sHHIIII", UHID_CREATE2,
            b"Pro Controller", f"ngc/{self.mac}".encode(), self.mac.lower().encode(),
            len(rd), BUS_BLUETOOTH, PROCON_VID, PROCON_PID, 0x0001, 0,
        ) + rd
        os.write(self._fd, create)
        logger.info("created virtual Pro Controller for %s (%s)", name, self.mac)

        self._reader = threading.Thread(target=self._read_loop, name="uhid-read", daemon=True)
        self._ticker = threading.Thread(target=self._tick_loop, name="uhid-tick", daemon=True)
        self._reader.start()
        self._ticker.start()

    # ------------------------------------------------------------------ #
    # Input side (called from the BLE reader thread)                      #
    # ------------------------------------------------------------------ #

    def update(self, buttons: int, left_stick, right_stick, left_trigger: int = 0,
               right_trigger: int = 0) -> None:
        for src, dst in self._extra:
            if buttons & src:
                buttons |= dst
        b = bytes([buttons & 0xFF, (buttons >> 8) & 0x3F, (buttons >> 16) & 0xFF])
        left = stick_raw(*left_stick)
        right = stick_raw(*right_stick)
        with self._lock:
            self._buttons, self._left, self._right = b, left, right

    def update_motion(self, report: "P.InputReport") -> None:
        self._detect_gyro_scale(report)
        a0, a1, a2 = report.accel
        # Remove the factory zero-rate bias, then express the sample in the
        # fixed scale the emulated calibration advertises (GYRO_REF_COEFF).
        k = self.gyro_coeff / 32767.0
        rescale = self.gyro_coeff / GYRO_REF_COEFF
        g0, g1, g2 = (
            (raw - bias / k) * rescale
            for raw, bias in zip(report.gyro, self.gyro_bias)
        )
        # Switch 2 raw (x, y, z) -> original Pro Controller (x, y, z) = (y, -x, z).
        vals = (a1, -a0, a2, g1, -g0, g2)
        imu = struct.pack("<6h", *(max(-32768, min(32767, int(round(v)))) for v in vals))
        with self._lock:
            self._imu = imu
            self._battery = battery_nibble(report.battery_mv)

    def _detect_gyro_scale(self, report: "P.InputReport") -> None:
        if self._ts_done or len(report.raw) < 0x2E:
            return
        ts = P.decodeu(report.raw[0x2A:0x2E])
        if not ts:
            return
        now = time.monotonic()
        if self._ts_first is None:
            self._ts_first = (now, ts)
            return
        self._ts_samples += 1
        host_us = (now - self._ts_first[0]) * 1e6
        if host_us < 2_000_000:
            return
        ratio = ((ts - self._ts_first[1]) & 0xFFFFFFFF) / host_us
        self.gyro_coeff = 34.8 if 0.8 <= ratio <= 1.25 else 40.0
        self._ts_done = True
        logger.info("sensor timestamp ratio %.3f -> gyro full scale %.1f rad/s", ratio, self.gyro_coeff)

    def release_all(self) -> None:
        with self._lock:
            self._buttons = bytes(3)
            self._left = stick_raw(0.0, 0.0)
            self._right = stick_raw(0.0, 0.0)

    # ------------------------------------------------------------------ #
    # Report construction                                                  #
    # ------------------------------------------------------------------ #

    def _header(self, report_id: int) -> bytearray:
        with self._lock:
            self._timer = (self._timer + 3) & 0xFF
            return bytearray(
                bytes([report_id, self._timer, (self._battery << 4) | 0x00])
                + self._buttons + self._left + self._right + b"\x80"
            )

    def _state_report(self) -> bytes:
        r = self._header(0x30)
        with self._lock:
            imu = self._imu
        r += imu * 3
        return bytes(r.ljust(REPORT_LEN, b"\x00"))

    def _reply(self, subcmd: int, ack: int, data: bytes = b"") -> bytes:
        r = self._header(0x21)
        r += bytes([ack, subcmd]) + data
        return bytes(r.ljust(REPORT_LEN, b"\x00")[:REPORT_LEN])

    def _send(self, report: bytes) -> bool:
        try:
            os.write(self._fd, struct.pack("<IH", UHID_INPUT2, len(report)) + report)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # Threads                                                              #
    # ------------------------------------------------------------------ #

    def _tick_loop(self) -> None:
        self._started.wait(5.0)
        next_at = time.monotonic()
        while self._running:
            self._send(self._state_report())
            # Rumble needs refreshing by the host; stop if it goes quiet.
            if self._last_rumble != (0.0, 0.0) and time.monotonic() - self._last_rumble_at > 1.0:
                self._apply_rumble(0.0, 0.0)
            next_at += REPORT_PERIOD_S
            delay = next_at - time.monotonic()
            if delay < -0.1:          # fell behind (suspend, stall): resync
                next_at = time.monotonic()
            elif delay > 0:
                time.sleep(delay)

    def _read_loop(self) -> None:
        while self._running:
            try:
                ev = os.read(self._fd, UHID_EVENT_SIZE)
            except OSError:
                break
            if len(ev) < 4:
                continue
            etype = struct.unpack_from("<I", ev, 0)[0]
            try:
                if etype == UHID_START:
                    self._started.set()
                elif etype == UHID_OUTPUT:
                    size = struct.unpack_from("<H", ev, 4 + UHID_DATA_MAX)[0]
                    self._handle_output(ev[4:4 + size])
                elif etype == UHID_GET_REPORT:
                    rid = struct.unpack_from("<I", ev, 4)[0]
                    os.write(self._fd, struct.pack("<IIHH", UHID_GET_REPORT_REPLY, rid, 5, 0))
                elif etype == UHID_SET_REPORT:
                    rid = struct.unpack_from("<I", ev, 4)[0]
                    os.write(self._fd, struct.pack("<IIH", UHID_SET_REPORT_REPLY, rid, 5))
            except Exception as exc:  # noqa: BLE001
                logger.debug("uhid event %d failed: %s", etype, exc)

    # ------------------------------------------------------------------ #
    # Host -> controller                                                   #
    # ------------------------------------------------------------------ #

    def _apply_rumble(self, low: float, high: float) -> None:
        self._last_rumble = (low, high)
        self._last_rumble_at = time.monotonic()
        cb = self.rumble_cb
        if cb is not None:
            try:
                cb(low, high)
            except Exception as exc:  # noqa: BLE001
                logger.debug("rumble callback failed: %s", exc)

    def _handle_output(self, data: bytes) -> None:
        if not data:
            return
        rid = data[0]
        if rid not in (0x01, 0x10) or len(data) < 10:
            return  # 0x80 USB commands get no answer, which tells hosts this is Bluetooth
        low, high = decode_rumble(data[2:10])
        if (low, high) != (0.0, 0.0) or self._last_rumble != (0.0, 0.0):
            self._apply_rumble(low, high)
        if rid == 0x01 and len(data) >= 11:
            self._handle_subcommand(data[10], data[11:])

    def _handle_subcommand(self, sub: int, arg: bytes) -> None:
        ack, payload = 0x80, b""
        if sub == 0x02:    # device info
            ack = 0x82
            payload = bytes([0x04, 0x33, 0x03, 0x02]) + bytes.fromhex(self.mac.replace(":", "")) + b"\x01\x01"
        elif sub == 0x10:  # SPI flash read
            if len(arg) >= 5:
                addr, length = struct.unpack_from("<IB", arg, 0)
                length = min(length, 0x1D)
                ack = 0x90
                payload = struct.pack("<IB", addr, length) + self.flash.read(addr, length)
        elif sub == 0x01:  # manual pairing
            ack, payload = 0x81, b"\x03"
        elif sub == 0x04:  # trigger buttons elapsed time
            ack, payload = 0x83, bytes(14)
        elif sub == 0x50:  # regulated voltage
            ack, payload = 0xD0, struct.pack("<H", 1600)
        elif sub == 0x21:  # MCU config
            ack, payload = 0xA0, bytes([0x01, 0x00, 0xFF, 0x00, 0x08, 0x00, 0x1B, 0x01])
        elif sub == 0x30:  # player lights: low nibble solid, high nibble flashing
            if arg:
                mask = (arg[0] & 0x0F) or (arg[0] >> 4)
                self._async(self.led_cb, mask)
        elif sub == 0x06:  # HCI state: 0 = disconnect / sleep
            self._send(self._reply(sub, ack))
            self._async(self.power_off_cb)
            return
        self._send(self._reply(sub, ack, payload))

    @staticmethod
    def _async(cb, *args) -> None:
        if cb is None:
            return
        threading.Thread(target=lambda: _quiet(cb, *args), daemon=True).start()

    def close(self) -> None:
        if not self._running:
            return
        self._running = False
        if self.rumble_cb is not None:
            try:
                self.rumble_cb(0.0, 0.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.write(self._fd, struct.pack("<I", UHID_DESTROY))
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        logger.info("removed virtual Pro Controller for %s", self.mac)


def _quiet(cb, *args) -> None:
    try:
        cb(*args)
    except Exception as exc:  # noqa: BLE001
        logger.debug("callback failed: %s", exc)


class UhidMotion:
    """Adapter so the bridge can treat motion like its evdev IMU node."""

    def __init__(self, pad: ProconUHID):
        self.pad = pad

    def update(self, report: "P.InputReport") -> None:
        self.pad.update_motion(report)

    def close(self) -> None:
        pass
