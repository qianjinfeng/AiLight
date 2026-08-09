"""
sn902.py - SN902W / SN902B (Sleepace "Nox 902W / Nox 902B Music") BLE protocol.

The Sleepace Nox2 device exposes a BLE GATT service and a binary "Nox2Packet"
framing protocol. This module implements the framing + the subset of commands
needed to use the device as an RGBW status light:

  * connect to the BLE device
  * user-config / login handshake (best effort)
  * CMD_LIGHT (0x30): set main light to an arbitrary RGB(W) color
  * CMD_LIGHT (0x30): turn the light off

Protocol reference (reverse-engineered from the Sleepace app v3.8.11,
com.medica.xiangshui, package com.medica.xiangshui.devicemanager):

  Constants.java
      BLE_WRITE_SERVER_UUID        0000ffe5-0000-1000-8000-00805f9b34fb
      BLE_WRITE_CHARACTERISTIC_UUID 0000ffe9-0000-1000-8000-00805f9b34fb
      BLE_NOTIFY_SERVER_UUID       0000ffe0-0000-1000-8000-00805f9b34fb
      BLE_NOTIFY_CHARACTERISTIC_UUID 0000ffe4-0000-1000-8000-00805f9b34fb

  Nox2Packet.java (frame = head(7) + body + CRC32(4) + tail(4))
      head:  version(1)=0, type(1)=2(FA_REQUEST), btCount(1)=1, btIndex(1)=0,
             sequence(1), deviceType(2)=11 (Nox2B)
      body:  msgType(1) + content
      crc:   CRC32 over head+body (big-endian, java.util.zip.CRC32 == zlib.crc32)
      tail:  fixed 4 bytes 24 5F 27 2D

  LightOperationReq (Nox2Packet.java) for msgType 48 (CMD_LIGHT):
      content[0] = operation | (ctrlMode << 4)
                    open=1 / close=0 / brightness=2, ctrlMode LIGHT=1
      content[1] = brightness            (0-255)
      content[2] = lightMode             (0=white, 1=RGB color)
      content[3..6] = r, g, b, w

  UserCfgReq (msgType 32): content = [1, userId:int32BE, language]
  TimeSyncReq (msgType 16): content = [timestamp:int32, tzOffsetSec:int32, 0, 0]
"""

import asyncio
import logging
import struct
import time
import zlib

try:
    import bleak
    BLEAK_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    BLEAK_AVAILABLE = False

log = logging.getLogger("sn902")

# ---- GATT identifiers -------------------------------------------------------
BLE_WRITE_SERVICE_UUID = "0000ffe5-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_WRITE_CHARACTERISTIC_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_CHARACTERISTIC_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"

# ---- device types -----------------------------------------------------------
DEVICE_TYPE_NOX_2B = 11  # Nox 902B Music (BLE only)
DEVICE_TYPE_NOX_2W = 12  # Nox 902W (BLE + WiFi)

# advertised name fragments used to recognise the device while scanning.
# SN902B -> deviceId "SN-21XXXXXXXXX"; SN902W -> deviceId "SN22XXXXXXXXX".
NAME_FRAGMENTS = ("SN22", "SN-21", "SN", "Nox 902", "902B", "902W")

# Nox 902W units were observed advertising these service UUIDs even though
# they expose the ffe0/ffe5 GATT services after connection. Any device
# advertising one of these is treated as a connection candidate.
MATCH_SERVICES = ("0000fff0", "0000ffb0")

# ---- packet header types (DataPacket.PacketType) -----------------------------
FA_REQUEST = 2
FA_POST = 1
FA_RESPONSE = 3

# ---- body/message types (Nox2Packet.PacketMsgType) ---------------------------
MSG_DEVICE_INFO = 17
MSG_TIME_SYNC = 16
MSG_USER_CFG = 32        # login / user-config
MSG_LIGHT = 48           # CMD_LIGHT - main light
MSG_SET_LIGHT_NIGHT = 38 # small night light

# ---- light operations (INoxManager.PostLightControl) --------------------------
LIGHT_CLOSE = 0
LIGHT_OPEN = 1
LIGHT_BRIGHTNESS = 2

# ---- control mode (INoxManager.SleepAidCtrlMode) ------------------------------
CTRL_LIGHT = 1

# ---- light mode (NoxLight.LightMode) ------------------------------------------
LIGHT_MODE_WHITE = 0
LIGHT_MODE_COLOR = 1

# frame tail signature written by Nox2Packet.fill():
#   {SET_GESTURE=36, LOG_GET=95, SleepDot BATTARY_QUERY=64, SET_AROMATHERAY_TIMER=45}
_TAIL = bytes([0x24, 0x5F, 0x40, 0x2D])


# ------------------------------------------------------------------------------
# pure protocol helpers (no bleak dependency - unit-testable)
# ------------------------------------------------------------------------------
def build_packet(msg_type, content=b"", seq=0, device_type=DEVICE_TYPE_NOX_2B,
                 head_type=FA_REQUEST):
    """Build one Nox2Packet frame as a bytes object."""
    head = struct.pack(">BBBBBh", 0, head_type, 1, 0, seq & 0xFF, device_type)
    body = bytes([msg_type & 0xFF]) + bytes(content)
    payload = head + body
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return payload + struct.pack(">I", crc) + _TAIL


def parse_response(data):
    """Best-effort parse of a notify frame. Returns dict or None."""
    if data is None or len(data) < 12:
        return None
    b = bytes(data)
    # find the tail signature; the frame ends with it
    idx = b.rfind(_TAIL)
    if idx < 12:
        return None
    frame = b[:idx]
    crc = struct.unpack(">I", frame[-4:])[0] if len(frame) >= 4 else None
    if len(frame) >= 8:
        seq = frame[4]
        device_type = struct.unpack(">h", frame[5:7])[0]
        body_type = frame[7]
        rsp_code = frame[8] if len(frame) > 8 else None
        return {
            "seq": seq, "device_type": device_type,
            "body_type": body_type, "rsp_code": rsp_code,
            "crc": crc, "raw": b,
        }
    return None


# ---- content builders ---------------------------------------------------------
def light_open_content(r, g, b, w=0, brightness=50, light_mode=LIGHT_MODE_COLOR):
    op = (LIGHT_OPEN | (CTRL_LIGHT << 4)) & 0xFF
    return bytes([op, brightness & 0xFF, light_mode & 0xFF,
                  r & 0xFF, g & 0xFF, b & 0xFF, w & 0xFF])


def light_brightness_content(r, g, b, w, brightness=50, light_mode=LIGHT_MODE_COLOR):
    op = (LIGHT_BRIGHTNESS | (CTRL_LIGHT << 4)) & 0xFF
    return bytes([op, brightness & 0xFF, light_mode & 0xFF,
                  r & 0xFF, g & 0xFF, b & 0xFF, w & 0xFF])


def light_close_content():
    return bytes([(LIGHT_CLOSE | (CTRL_LIGHT << 4)) & 0xFF])


def user_cfg_content(user_id=1, language=0):
    return struct.pack(">BiB", 1, int(user_id) & 0xFFFFFFFF, language & 0xFF)


def time_sync_content(now=None, tz_offset_sec=0):
    now = int(time.time()) if now is None else int(now)
    return struct.pack(">IiBi", now, int(tz_offset_sec), 0, 0)


def night_light_content(light_flag=1, brightness=30, r=0, g=0, b=0, w=255,
                        start_hour=0, start_minute=0, continue_time_min=0):
    return bytes([1, light_flag & 0xFF, brightness & 0xFF,
                  r & 0xFF, g & 0xFF, b & 0xFF, w & 0xFF,
                  start_hour & 0xFF, start_minute & 0xFF]) + \
        struct.pack(">h", continue_time_min)


def hsv_to_rgb(h, s=1.0, v=1.0):
    """h in [0,360), s/v in [0,1] -> (r,g,b) 0-255."""
    h = float(h) % 360.0
    s = max(0.0, min(1.0, float(s)))
    v = max(0.0, min(1.0, float(v)))
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return int(round((r + m) * 255)), int(round((g + m) * 255)), int(round((b + m) * 255))


# ------------------------------------------------------------------------------
# async BLE client
# ------------------------------------------------------------------------------
class SN902Device:
    """Async BLE client for one SN902W/SN902B device."""

    def __init__(self, on_notify=None, dry_run=False):
        self._on_notify = on_notify
        self.dry_run = dry_run
        self.client = None
        self.write_char_uuid = None
        self.notify_char_uuid = None
        self._seq = 0
        self.connected = False
        self.connected_at = None
        self.device_name = None
        self.device_address = None
        self.device_type = DEVICE_TYPE_NOX_2W
        self.last_error = None
        self._write_lock = asyncio.Lock()

    # -- helpers ---------------------------------------------------------------
    @property
    def seq(self):
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _log_hex(self, pkt, prefix="<<"):
        log.debug("%s %s", prefix, pkt.hex())

    # -- connection -------------------------------------------------------------
    @staticmethod
    async def scan(timeout=8.0, name_fragments=NAME_FRAGMENTS, address=None):
        """
        Scan for BLE devices using a detection callback (captures full
        advertisement data: local name, rssi, manufacturer data, services).

        Returns `(found, all_devices, adapter_ok, adapter_error)`:
          - found:       (name, address, rssi) matching `name_fragments`,
                         the pinned `address`, or an advertised MATCH_SERVICES uuid
          - all_devices: everything discovered, as
                         (name, address, rssi, manufacturer_data, service_uuids)
          - adapter_ok / adapter_error: False when the OS reports no Bluetooth
            radio (then device-not-found is expected).
        """
        found = []
        all_devices = []
        adapter_ok = True
        adapter_error = ""
        seen = set()

        def _match(name, svcs):
            if address and False:  # handled below by exact address
                return False
            if name and any(f in name for f in name_fragments):
                return True
            for u in svcs:
                ul = u.lower()
                if any(ul.startswith(s) for s in MATCH_SERVICES):
                    return True
            return False

        def callback(device, adv):
            key = device.address
            if key in seen:
                return
            seen.add(key)
            name = (getattr(adv, "local_name", None) or device.name or "").strip()
            rssi = getattr(adv, "rssi", None)
            svcs = list(getattr(adv, "service_uuids", None) or [])
            entry = (name, device.address, rssi,
                     getattr(adv, "manufacturer_data", None) or {}, svcs)
            all_devices.append(entry)
            if address and device.address.lower() == str(address).lower():
                found.append(entry[:3])
            elif _match(name, svcs):
                found.append(entry[:3])

        try:
            scanner = bleak.BleakScanner(detection_callback=callback)
            await scanner.start()
            await asyncio.sleep(timeout)
            await scanner.stop()
        except Exception as e:  # pragma: no cover
            adapter_ok = False
            adapter_error = str(e)
            log.warning("scan failed: %s", e)
            return found, all_devices, adapter_ok, adapter_error
        return found, all_devices, adapter_ok, adapter_error

    async def connect(self, target):
        """
        Connect to a device. `target` is either a BLE address (str) or a
        (name, address) tuple. In dry_run mode this simply marks connected.
        """
        if self.dry_run:
            self.connected = True
            self.connected_at = time.time()
            self.device_address = str(target)
            self.device_name = "SN902W (dry-run)"
            log.info("dry-run: connected (no real BLE)")
            return True
        if not BLEAK_AVAILABLE:
            self.last_error = "bleak is not installed (pip install bleak)"
            log.error(self.last_error)
            return False

        address = target if isinstance(target, str) else target[1]
        name = None if isinstance(target, str) else target[0]

        self.client = bleak.BleakClient(address)
        try:
            await self.client.connect(timeout=20.0)
        except Exception as e:
            self.last_error = "connect failed: %s" % e
            log.error(self.last_error)
            return False

        self.device_address = address
        self.device_name = name or self.client.address

        # discover characteristics
        found_write = found_notify = None
        try:
            for service in self.client.services:
                for ch in service.characteristics:
                    uuid = ch.uuid.lower()
                    if uuid == BLE_WRITE_CHARACTERISTIC_UUID:
                        found_write = ch
                    elif uuid == BLE_NOTIFY_CHARACTERISTIC_UUID:
                        found_notify = ch
        except Exception as e:  # pragma: no cover
            self.last_error = "service discovery failed: %s" % e
            log.error(self.last_error)
            await self.disconnect()
            return False

        if found_write is None:
            # some firmware revisions only use the ffe5/ffe9 service UUID form
            for service in self.client.services:
                for ch in service.characteristics:
                    u = ch.uuid.lower()
                    if u.startswith("0000ffe5") or u.startswith("0000ffe9"):
                        if "ffe9" in u and found_write is None:
                            found_write = ch
                        elif "ffe5" in u and found_write is None:
                            found_write = ch

        if found_write is None or found_notify is None:
            self.last_error = "required characteristics not found (write=%s notify=%s)" % (
                found_write is not None, found_notify is not None)
            log.error(self.last_error)
            await self.disconnect()
            return False

        self.write_char_uuid = found_write.uuid
        self.notify_char_uuid = found_notify.uuid

        try:
            await self.client.start_notify(self.notify_char_uuid, self._handle_notify)
        except Exception as e:  # pragma: no cover
            log.warning("start_notify failed (continuing): %s", e)

        self.connected = True
        self.connected_at = time.time()
        log.info("connected to %s (%s)", self.device_name, self.device_address)
        return True

    def _handle_notify(self, _char, data):
        parsed = parse_response(data)
        if parsed:
            log.debug("notify: %s", parsed)
        elif self._on_notify:
            pass
        if self._on_notify:
            try:
                self._on_notify(parsed)
            except Exception:  # pragma: no cover
                log.exception("notify callback failed")

    async def disconnect(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.connected = False
        self.write_char_uuid = None
        self.notify_char_uuid = None

    # -- writes ------------------------------------------------------------------
    async def write(self, msg_type, content=b"", head_type=FA_REQUEST,
                    chunk=20):
        """Send one Nox2Packet frame (chunked to <=20 bytes like the app)."""
        pkt = build_packet(msg_type, content, self.seq, head_type=head_type)
        self._log_hex(pkt, "<<")
        if self.dry_run:
            log.info("dry-run packet msg=%s len=%d %s", msg_type, len(pkt), pkt.hex())
            return pkt
        if not self.connected or self.client is None:
            raise ConnectionError("device not connected")
        async with self._write_lock:
            for i in range(0, len(pkt), chunk):
                piece = pkt[i:i + chunk]
                try:
                    await self.client.write_gatt_char(self.write_char_uuid, piece)
                except Exception:
                    # some Windows/device combos reject write-with-response
                    try:
                        await self.client.write_gatt_char(self.write_char_uuid, piece, response=False)
                    except Exception as e:
                        raise ConnectionError("write failed: %s" % e)
                await asyncio.sleep(0.02)
        return pkt

    # -- high level commands ------------------------------------------------------
    async def login(self, user_id=1, language=0):
        return await self.write(MSG_USER_CFG, user_cfg_content(user_id, language))

    async def sync_time(self, tz_offset_sec=None):
        if tz_offset_sec is None:
            tz_offset_sec = -time.timezone if time.localtime().tm_isdst == 0 else -time.timezone
        return await self.write(MSG_TIME_SYNC, time_sync_content(tz_offset_sec=tz_offset_sec))

    async def set_color(self, r, g, b, w=0, brightness=50, light_mode=LIGHT_MODE_COLOR):
        return await self.write(MSG_LIGHT, light_open_content(r, g, b, w, brightness, light_mode))

    async def set_white(self, w, brightness=50):
        return await self.write(MSG_LIGHT, light_open_content(0, 0, 0, w, brightness, LIGHT_MODE_WHITE))

    async def set_brightness(self, r, g, b, w, brightness=50, light_mode=LIGHT_MODE_COLOR):
        return await self.write(MSG_LIGHT, light_brightness_content(r, g, b, w, brightness, light_mode))

    async def light_off(self):
        return await self.write(MSG_LIGHT, light_close_content())

    async def night_light(self, on=True, brightness=30, r=0, g=0, b=0, w=255):
        return await self.write(MSG_SET_LIGHT_NIGHT,
                                night_light_content(light_flag=1 if on else 0,
                                                    brightness=brightness, r=r, g=g, b=b, w=w))
