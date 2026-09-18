"""BLE session management for Tuya BLE lock integration.

Adapted from lock_control.py LockSession + connect_and_setup().
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import struct
import time
from typing import Callable

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import ble_protocol
from .ble_protocol import (
    parse_dp_report,
    parse_dp_report_v3,
    parse_event_record,
    parse_frames,
)
from .const import (
    WRITE_UUID,
    NOTIFY_UUID,
    CMD_DEVICE_INFO,
    CMD_PAIR,
    CMD_DP_WRITE_V3,
    CMD_DP_WRITE_V4,
    CMD_DEVICE_STATUS,
    CMD_TIME_V1,
    CMD_TIME_V2,
    CMD_RECV_DP,
    CMD_DP_REPORT_V4,
    CMD_DP_EVENT_V4,
    SEC_NONE,
    SEC_AUTH_KEY,
    SEC_AUTH_SESSION,
    SEC_ENCRYPTED_AUTH_KEY,
    SEC_LOGIN_KEY,
    SEC_SESSION_KEY,
    SEC_NEW_PAIR,
    SEC_NEW_SEC,
    SEC_NEW_SEC_SESSION,
)

_LOGGER = logging.getLogger(__name__)


def _build_v5_pair_payload(
    *,
    local_key: bytes,
    sec_key: bytes,
    dev_id: str,
    dev_uuid: str,
    verify_key: bytes,
    need_beacon: bool,
    support_struct_dp: bool,
    beacon_key: bytes | None = None,
) -> bytes:
    def _pad(data: bytes, length: int, pad: int) -> bytes:
        return data[:length] + bytes([pad]) * max(0, length - len(data))

    payload = bytearray()
    payload += _pad(dev_uuid.encode(), 16, 0xFF)
    payload += local_key[:6]
    payload += _pad(dev_id.encode(), 22, 0x00)
    if need_beacon and beacon_key:
        payload += bytes([len(beacon_key)]) + _pad(beacon_key, 16, 0x00)
    else:
        payload += b"\x00"
    payload += b"\x01"
    payload += local_key[:16]
    payload += sec_key[:16]
    payload += (verify_key + b"\x00\x00\x00\x00")[:4]
    payload += b"\x01\x01" if support_struct_dp else b"\x00"
    payload += b"\x00\x00"
    return bytes(payload)


def _parse_v5_device_info(data: bytes) -> dict:
    if len(data) < 12:
        raise ValueError(f"V5 device info too short: {len(data)} bytes")
    proto_index = data[2] * 10 + data[3]
    flag = data[4]
    support_struct_dp = None
    if proto_index >= 48 and len(data) > 85:
        support_struct_dp = ((data[85] >> 3) & 1) == 1
    return {
        "flag": flag,
        "is_bound": data[5] == 1,
        "srand": data[6:12],
        "need_beacon": (flag & 0x10) != 0,
        "support_struct_dp": support_struct_dp,
    }


class DeviceAlreadyBoundError(Exception):
    """Raised when device is already bound and needs existing credentials or factory reset."""


class PairingFailedError(Exception):
    """Raised when the lock does not accept or complete PAIR."""


class BindVerificationError(Exception):
    """Raised when PAIR succeeds but the bound state cannot be verified."""


class TuyaBLELockSession:
    """Manage a BLE connection and protocol state with a Tuya lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device,
        login_key: bytes,
        virtual_id: bytes,
        device_uuid: str,
        auth_key: bytes | None = None,
        auth_random: bytes | None = None,
        protocol_version: int = 4,
        local_key: bytes | None = None,
        sec_key: bytes | None = None,
        verify_key: bytes | None = None,
        check_code: str | None = None,
    ):
        self._hass = hass
        self._ble_device = ble_device
        self._login_key = login_key
        self._virtual_id = virtual_id
        self._device_uuid = device_uuid
        self._auth_key = auth_key
        self._auth_random = auth_random
        self._protocol_version = protocol_version
        # btScyChannel ("new security") credentials: when both local_key and sec_key
        # are set, session uses sec_flags 14/15 instead of 4/5.
        self._local_key = local_key
        self._sec_key = sec_key
        self._verify_key = verify_key or b"\x00\x00\x00\x00"
        self._check_code = check_code or ""
        self._btsc = bool(local_key and sec_key)
        self._client: BleakClient | None = None
        self._seq = ble_protocol.SequenceCounter()
        self._keys: dict[int, bytes] = {}
        if self._btsc:
            # KEY14 = MD5(local_key + sec_key) — used for DEVICE_INFO.
            # KEY15 derived after DEVICE_INFO response provides srand.
            self._keys[SEC_NEW_SEC] = hashlib.md5(
                self._local_key + self._sec_key
            ).digest()
        if login_key:
            self._keys[SEC_LOGIN_KEY] = hashlib.md5(login_key).digest()
        if auth_key:
            self._keys[SEC_AUTH_KEY] = auth_key
            self._keys[SEC_ENCRYPTED_AUTH_KEY] = auth_key
        self._session_key: bytes | None = None
        self._notif_buf: list[bytes] = []
        self.is_connected = False
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._dp_report_callback: Callable[[list[dict]], None] | None = None
        self._write_uuid: str = WRITE_UUID
        self._notify_uuid: str = NOTIFY_UUID

    def _resolve_gatt_uuids(self) -> tuple[str | None, str | None]:
        """Find write and notify characteristic UUIDs from discovered services.

        Tries FD50 characteristics first, then falls back to scanning all services
        for any write-without-response + notify characteristic pair.
        Returns (write_uuid, notify_uuid) or (None, None) if not found.
        """
        if not self._client or not self._client.services:
            return None, None

        # Check if FD50 characteristics exist (get_characteristic returns None if not found)
        write_char = self._client.services.get_characteristic(WRITE_UUID)
        notify_char = self._client.services.get_characteristic(NOTIFY_UUID)
        if write_char is not None and notify_char is not None:
            self._write_uuid = WRITE_UUID
            self._notify_uuid = NOTIFY_UUID
            _LOGGER.debug(
                "Using FD50 GATT characteristics for %s", self._ble_device.address
            )
            return WRITE_UUID, NOTIFY_UUID

        # Fall back: scan all services for write-without-response + notify chars
        write_uuid = None
        notify_uuid = None
        for svc in self._client.services:
            for char in svc.characteristics:
                if "write-without-response" in char.properties and not write_uuid:
                    write_uuid = char.uuid
                if "notify" in char.properties and not notify_uuid:
                    notify_uuid = char.uuid
        if write_uuid and notify_uuid:
            self._write_uuid = write_uuid
            self._notify_uuid = notify_uuid
            _LOGGER.debug(
                "Using discovered GATT characteristics for %s: write=%s notify=%s",
                self._ble_device.address,
                write_uuid,
                notify_uuid,
            )
            return write_uuid, notify_uuid

        return None, None

    def set_dp_report_callback(self, callback: Callable[[list[dict]], None]) -> None:
        """Set a callback for unsolicited DP reports (push-based updates)."""
        self._dp_report_callback = callback

    # ---------- internal helpers ----------
    def _on_disconnect(self, client):
        _LOGGER.debug("BLE disconnected")
        self.is_connected = False

    def _on_notify(self, sender, data):
        _LOGGER.debug(
            "BLE NOTIFY received: len=%d hex=%s", len(data), bytes(data)[:40].hex()
        )
        self._notif_buf.append(bytes(data))

    def _derive_session(self, srand: bytes) -> None:
        """Derive session keys from srand.

        keys[5]  = MD5(login_key + srand) — for bound device reconnect
        keys[2]  = MD5(auth_key_hex + srand) — for first activation
        keys[15] = MD5(local_key + sec_key + srand) — btScyChannel session
        """
        self._session_key = hashlib.md5(self._login_key + srand).digest()
        self._keys[SEC_SESSION_KEY] = self._session_key
        if self._auth_key:
            combined = self._auth_key.hex().encode("ascii") + srand
            self._keys[SEC_AUTH_SESSION] = hashlib.md5(combined).digest()
            self._keys[SEC_NEW_PAIR] = hashlib.md5(self._auth_key + srand).digest()
        if self._btsc:
            self._keys[SEC_NEW_SEC_SESSION] = hashlib.md5(
                self._local_key + self._sec_key + srand
            ).digest()

    async def _send_encrypted(
        self,
        cmd: int,
        data: bytes,
        sec_flag: int,
        ack_sn: int = 0,
        fixed_iv: bytes | None = None,
    ):
        """Build encrypted fragments and write via GATT."""
        key = self._keys.get(sec_flag)
        if sec_flag != SEC_NONE and not key:
            raise RuntimeError(
                f"No key available for sec_flag={sec_flag}. "
                f"Available: {list(self._keys.keys())}. Session not established?"
            )
        if sec_flag == SEC_NONE:
            # Unencrypted: manually build frame + fragment
            frame = ble_protocol.TuyaBleFrame(
                sn=self._seq.next(), ack_sn=ack_sn, code=cmd, data=data
            )
            raw = frame.to_bytes()
            payload = bytes([SEC_NONE]) + raw
            writes = ble_protocol.fragment(
                payload, mtu=20, protocol_version=self._protocol_version
            )
        else:
            frame = ble_protocol.TuyaBleFrame(
                sn=self._seq.next(), ack_sn=ack_sn, code=cmd, data=data
            )
            raw = frame.to_bytes()
            encrypted = ble_protocol.encrypt_frame(key, sec_flag, raw, iv=fixed_iv)
            writes = ble_protocol.fragment(
                encrypted, mtu=20, protocol_version=self._protocol_version
            )
        _LOGGER.debug(
            "GATT WRITE cmd=0x%04x sec=%d frags=%d", cmd, sec_flag, len(writes)
        )
        for i, w in enumerate(writes):
            _LOGGER.debug("  frag[%d]: len=%d hex=%s", i, len(w), w.hex())
            await self._client.write_gatt_char(self._write_uuid, w, response=False)
            if i < len(writes) - 1:
                await asyncio.sleep(0.02)

    async def _send_recv(
        self, cmd: int, data: bytes, sec_flag: int, wait: float = 8.0
    ) -> list[dict]:
        """Send command and wait for response with proper fragment reassembly."""
        async with self._lock:
            self._notif_buf.clear()
            await self._send_encrypted(cmd, data, sec_flag)
            # Wait for notifications to arrive
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                await asyncio.sleep(0.15)
                if self._notif_buf:
                    await asyncio.sleep(0.3)
                    break
                if not self._client or not self._client.is_connected:
                    _LOGGER.debug("Client disconnected while waiting for response")
                    return []
            raw = list(self._notif_buf)
            self._notif_buf.clear()
            if not raw:
                _LOGGER.debug(
                    "No BLE notifications received for cmd=0x%04x (waited %.1fs)",
                    cmd,
                    wait,
                )
                return []
            _LOGGER.debug("Got %d raw notifications for cmd=0x%04x", len(raw), cmd)
            # Reassemble and decrypt all notifications at once
            frames = parse_frames(self._keys, raw)
            _LOGGER.debug(
                "Parsed %d frames from %d notifications for cmd=0x%04x: %s",
                len(frames),
                len(raw),
                cmd,
                [(f["cmd"], f.get("data", b"")[:20].hex()) for f in frames],
            )
            if not frames and raw:
                # We got data but couldn't decode — log reassembled payloads
                payloads = ble_protocol.reassemble(raw)
                for p in payloads:
                    _LOGGER.debug(
                        "Undecoded response: sec_flag=%d len=%d hex=%s",
                        p[0] if p else -1,
                        len(p),
                        p[:40].hex(),
                    )
            return frames

    @property
    def _session_sec(self) -> int:
        """Sec flag used for session-encrypted commands after PAIR."""
        return SEC_NEW_SEC_SESSION if self._btsc else SEC_SESSION_KEY

    async def _handle_time_requests(self, frames: list[dict]) -> None:
        """Answer what the device asks of us in unsolicited frames: time sync
        requests, and acknowledgements for DP reports that want one.

        The K3 marks its 0x8007 event records as needing an ack (b_type bit 7
        clear) and, unacked, retransmits each one three times about 6 s apart
        while apparently holding back the next event. The Tuya app acks them
        (DpsReportRep.needAck -> replayDpsReportAck); so do we.
        """
        for f in frames:
            cmd = f["cmd"]
            if cmd in (CMD_TIME_V1, CMD_TIME_V2):
                await self._send_encrypted(
                    cmd,
                    ble_protocol.build_time_payload(cmd),
                    self._session_sec,
                    ack_sn=f["sn"],
                )
            elif cmd in (CMD_DP_REPORT_V4, CMD_DP_EVENT_V4):
                ack = ble_protocol.build_report_ack(f.get("data", b""))
                if ack is None or not self._keys.get(self._session_sec):
                    continue
                _LOGGER.debug(
                    "ACK report cmd=0x%04x frame_sn=%d report_sn=%d",
                    cmd, f["sn"], int.from_bytes(ack[1:5], "big"),
                )
                await self._send_encrypted(
                    cmd, ack, self._session_sec, ack_sn=f["sn"]
                )

    async def _collect(self, timeout: float = 3.0) -> list[dict]:
        """Collect unsolicited reports (DP reports, time requests).

        Does NOT clear buffer at start — processes any pending data first.
        """
        async with self._lock:
            frames: list[dict] = []
            # Process any already-buffered notifications first
            if self._notif_buf:
                raw = list(self._notif_buf)
                self._notif_buf.clear()
                parsed = parse_frames(self._keys, raw)
                frames.extend(parsed)
                await self._handle_time_requests(parsed)
            # Then wait for more
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                if self._notif_buf:
                    await asyncio.sleep(0.3)
                    raw = list(self._notif_buf)
                    self._notif_buf.clear()
                    parsed = parse_frames(self._keys, raw)
                    frames.extend(parsed)
                    await self._handle_time_requests(parsed)
            return frames

    @staticmethod
    def _extract_dps_from_frame(f: dict) -> list[dict]:
        """Extract DPs from a single frame.

        State snapshots (cmd=0x8006) and event records (cmd=0x8007) both
        carry DP data — the latter being push notifications for things like
        keypad unlock attempts, wrong_finger / wrong_password alarms,
        doorbell presses and hijack triggers. Ignore either and you miss
        half of what the lock is telling you.
        """
        cmd = f["cmd"]
        if cmd == CMD_DP_REPORT_V4:
            return parse_dp_report(f["data"])
        if cmd == CMD_DP_EVENT_V4:
            return parse_event_record(f["data"])
        if cmd == CMD_RECV_DP:
            return parse_dp_report_v3(f["data"])
        return []

    def _dispatch_dp_reports(self, frames: list[dict]) -> None:
        """Extract DP reports from frames and push to coordinator callback."""
        if not self._dp_report_callback:
            return
        all_dps = []
        for f in frames:
            dps = self._extract_dps_from_frame(f)
            if dps:
                _LOGGER.debug(
                    "Extracted DPs from cmd=0x%04x: %s",
                    f["cmd"],
                    [(d["id"], d["raw"].hex()) for d in dps],
                )
            all_dps.extend(dps)
        if all_dps:
            self._dp_report_callback(all_dps)

    # ---------- public API ----------
    async def async_connect_single_attempt(self) -> bool:
        """One-shot connect: single attempt, no retries. For startup battery fetch."""
        async with self._connect_lock:
            return await self._async_connect_inner(max_attempts=1)

    async def async_connect(self) -> bool:
        """Connect to the lock using stored login_key (bound device reconnect).

        Retry logic mirrors the pairing flow — device may be asleep between ads.
        Uses _connect_lock to prevent concurrent connection attempts.
        """
        async with self._connect_lock:
            return await self._async_connect_inner()

    async def _async_connect_inner(self, max_attempts: int = 3) -> bool:
        if self.is_connected:
            return True

        mtu_data = struct.pack(">H", 20)
        srand = None

        for attempt in range(max_attempts):
            try:
                await self.async_disconnect()
                # Refresh BLE device object — stale scan data can cause silent failures
                fresh = bluetooth.async_ble_device_from_address(
                    self._hass, self._ble_device.address, connectable=True
                )
                if fresh:
                    self._ble_device = fresh
                # Log which BLE adapter/source will be used
                _details = getattr(self._ble_device, "details", None) or {}
                _source = (
                    _details.get("source")
                    if isinstance(_details, dict)
                    else getattr(_details, "source", None)
                )
                _LOGGER.debug(
                    "Reconnect attempt %d/%d for %s (source=%s, rssi=%s): connecting...",
                    attempt + 1,
                    max_attempts,
                    self._ble_device.address,
                    _source,
                    getattr(self._ble_device, "rssi", "?"),
                )
                self._client = await establish_connection(
                    client_class=BleakClient,
                    device=self._ble_device,
                    name="tuya_ble_access",
                    disconnected_callback=self._on_disconnect,
                    max_attempts=2,
                )
                # Log discovered services on first successful connection
                if self._client.services:
                    for svc in self._client.services:
                        chars = [
                            f"{c.uuid}({','.join(c.properties)})"
                            for c in svc.characteristics
                        ]
                        _LOGGER.debug("  GATT Service %s: %s", svc.uuid, chars)
                else:
                    _LOGGER.debug(
                        "No GATT services discovered for %s", self._ble_device.address
                    )
                # Resolve write/notify UUIDs — try FD50 first, fall back to A201
                write_uuid, notify_uuid = self._resolve_gatt_uuids()
                if not write_uuid or not notify_uuid:
                    _LOGGER.error(
                        "No compatible GATT characteristics found for %s. Services: %s",
                        self._ble_device.address,
                        [s.uuid for s in self._client.services]
                        if self._client.services
                        else "none",
                    )
                    await asyncio.sleep(2.0)
                    continue
                # Always try stop_notify first to release any stale subscription
                try:
                    await self._client.stop_notify(notify_uuid)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
                self._notif_buf.clear()
                _LOGGER.debug(
                    "Starting notify on %s for %s",
                    notify_uuid,
                    self._ble_device.address,
                )
                await self._client.start_notify(notify_uuid, self._on_notify)
                self.is_connected = True

                # Send DEVICE_INFO immediately. For btsc (new-security) devices
                # use sec_flag 14; for legacy devices try sec_flag 4 then 0.
                sec_flags_to_try = (
                    [SEC_NEW_SEC] if self._btsc else [SEC_LOGIN_KEY, SEC_NONE]
                )
                raw = []
                for try_sec in sec_flags_to_try:
                    _LOGGER.debug("Sending device_info (sec_flag=%d)", try_sec)
                    self._notif_buf.clear()
                    await self._send_encrypted(CMD_DEVICE_INFO, mtu_data, try_sec)
                    deadline_di = time.monotonic() + 3.0
                    while time.monotonic() < deadline_di:
                        await asyncio.sleep(0.1)
                        if self._notif_buf:
                            await asyncio.sleep(0.2)
                            break
                    if self._notif_buf:
                        raw = list(self._notif_buf)
                        self._notif_buf.clear()
                        break

                if not raw:
                    _LOGGER.debug("No device info response on attempt %d", attempt + 1)
                    await asyncio.sleep(2.0)
                    continue

                _LOGGER.debug(
                    "Got %d raw notifications for device info, sizes=%s",
                    len(raw),
                    [len(r) for r in raw[:10]],
                )
                frames = parse_frames(self._keys, raw)
                if not frames:
                    _LOGGER.debug(
                        "Could not decrypt device info response (attempt %d)",
                        attempt + 1,
                    )
                    await asyncio.sleep(2.0)
                    continue
                # Dispatch any DP reports that arrived with device info
                self._dispatch_dp_reports(frames)

                for f in frames:
                    if f["cmd"] == CMD_DEVICE_INFO and len(f["data"]) >= 12:
                        srand = f["data"][6:12]
                        _LOGGER.debug("Device info OK, srand=%s", srand.hex())
                        break
                if srand:
                    break
            except Exception as exc:
                _LOGGER.debug(
                    "Reconnect attempt %d for %s failed: %s",
                    attempt + 1,
                    self._ble_device.address,
                    exc,
                )
                await asyncio.sleep(2.0)

        if not srand:
            _LOGGER.debug(
                "No device info response from %s after %d reconnect attempt(s)",
                self._ble_device.address,
                max_attempts,
            )
            self.is_connected = False
            return False

        self._derive_session(srand)
        await self._handle_time_requests(frames)

        # Build PAIR payload. Legacy: uuid(16)+login6+virtual(22) padded to 44.
        # btScyChannel: uuid(16)+login6+virtual(22)+local_key(16)+sec_key(16) = 76.
        uuid_bytes = self._device_uuid.encode()[:16]
        pair_data = uuid_bytes + self._login_key + self._virtual_id[:22]
        if self._btsc:
            pair_data += self._local_key[:16] + self._sec_key[:16]
        else:
            pair_data = (pair_data + b"\x00" * 44)[:44]

        pair_sec = self._session_sec
        self._notif_buf.clear()
        await self._send_encrypted(CMD_PAIR, pair_data, pair_sec)

        # Poll for PAIR response; also handle TIME_V1 (lock sends it right after PAIR)
        deadline_pr = time.monotonic() + 4.0
        pair_ok = False
        while time.monotonic() < deadline_pr:
            await asyncio.sleep(0.15)
            if self._notif_buf:
                raw_p = list(self._notif_buf)
                self._notif_buf.clear()
                pair_frames = parse_frames(self._keys, raw_p)
                await self._handle_time_requests(pair_frames)
                self._dispatch_dp_reports(pair_frames)
                for pf in pair_frames:
                    if pf["cmd"] == CMD_PAIR and pf.get("data", b"")[:1] in (
                        b"\x00",
                        b"\x02",
                    ):
                        pair_ok = True
                if pair_ok:
                    break
        if not pair_ok:
            _LOGGER.debug("No explicit PAIR OK seen (may still be usable)")

        # Arm the lock's event-push mode. Without these two writes the K3
        # only pushes motor_state + successful unlocks — alarm_lock
        # (wrong_finger, wrong_password, pry, …), hijack and doorbell
        # events stay silent. The Tuya app performs this exact sequence
        # right after PAIR (verified via btsnoop decode + live repro).
        if self._btsc:
            await self._arm_event_push()

        _LOGGER.info("BLE session established with %s", self._ble_device.address)
        return True

    async def _arm_event_push(self) -> None:
        """Tell the lock we want live event push.

        Two fire-and-forget writes, ~30 bytes each:
          * DP 64 (Offline Code Time) = current unix timestamp as ASCII.
          * DP 69 (Get Records)       = ffff 0001 [check_code] 00.
        Enables wrong_finger / wrong_password / hijack / doorbell pushes
        via cmd=0x8007 for the rest of the session. No battery cost
        beyond the two extra writes.
        """
        try:
            now_ts = str(int(time.time())).encode("ascii")
            payload = ble_protocol.build_v4_dp(64, 3, now_ts)
            await self._send_encrypted(CMD_DP_WRITE_V4, payload, self._session_sec)
            await asyncio.sleep(0.3)

            code_str = getattr(self, "_check_code", "") or ""
            code = (code_str.encode("ascii") + b"\x00" * 8)[:8]
            dp69_val = struct.pack(">HH", 0xFFFF, 1) + code + b"\x00"
            payload = ble_protocol.build_v4_dp(69, 0, dp69_val)
            await self._send_encrypted(CMD_DP_WRITE_V4, payload, self._session_sec)
            await asyncio.sleep(0.3)
            _LOGGER.debug("Event-push arm sent (DP64 + DP69)")
        except Exception as exc:
            _LOGGER.debug("Event-push arm failed: %s", exc)

    async def async_disconnect(self) -> None:
        if self._client:
            try:
                await self._client.stop_notify(self._notify_uuid)
            except Exception:
                pass
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self.is_connected = False

    def _build_dp_payload(
        self, dp_id: int, dp_type: int, value: bytes
    ) -> tuple[int, bytes]:
        """Build DP write command + payload for the correct protocol version.

        V3 (service 1910): cmd=0x0002, KLV=[dp_id:1][type:1][len:1][val]
        V4 (service FD50): cmd=0x0027, KLV=[hdr:5][dp_id:1][type:1][len:2][val]
        """
        if self._protocol_version <= 3:
            return CMD_DP_WRITE_V3, ble_protocol.build_v3_dp(dp_id, dp_type, value)
        return CMD_DP_WRITE_V4, ble_protocol.build_v4_dp(dp_id, dp_type, value)

    async def async_send_dp_fire_and_forget(
        self, dp_id: int, dp_type: int, value: bytes
    ) -> None:
        """Send a DP write without waiting for response. Used for lock/unlock."""
        cmd, payload = self._build_dp_payload(dp_id, dp_type, value)
        async with self._lock:
            await self._send_encrypted(cmd, payload, self._session_sec)
            # Brief wait to ensure BLE write completes before disconnect
            await asyncio.sleep(0.3)

    async def async_send_dp(
        self, dp_id: int, dp_type: int, value: bytes
    ) -> dict | None:
        """Send a DP write and return the matching DP from the response."""
        cmd, payload = self._build_dp_payload(dp_id, dp_type, value)
        frames = await self._send_recv(cmd, payload, self._session_sec)
        await self._handle_time_requests(frames)
        self._dispatch_dp_reports(frames)
        for f in frames:
            for dp in self._extract_dps_from_frame(f):
                if dp["id"] == dp_id:
                    return dp
        # Also collect follow-up reports
        extra = await self._collect(timeout=1.0)
        self._dispatch_dp_reports(extra)
        for f in extra:
            for dp in self._extract_dps_from_frame(f):
                if dp["id"] == dp_id:
                    return dp
        return None

    async def async_send_dp_bool(self, dp_id: int, value: bool) -> bool:
        val = b"\x01" if value else b"\x00"
        dp = await self.async_send_dp(dp_id, 1, val)
        return dp is not None

    async def async_send_dp_raw(self, dp_id: int, payload: bytes) -> dict | None:
        return await self.async_send_dp(dp_id, 0, payload)

    async def async_query_status(self) -> list[dict]:
        """Send CMD_DEVICE_STATUS, collect all DP reports."""
        _LOGGER.debug(
            "Sending status query (CMD=0x%04x, sec=%d)",
            CMD_DEVICE_STATUS,
            self._session_sec,
        )
        frames = await self._send_recv(CMD_DEVICE_STATUS, b"", self._session_sec)
        await self._handle_time_requests(frames)
        results: list[dict] = []
        for f in frames:
            _LOGGER.debug(
                "Status frame: cmd=0x%04x data=%s",
                f["cmd"],
                f.get("data", b"")[:40].hex(),
            )
            results.extend(self._extract_dps_from_frame(f))
        # Collect follow-up DP reports (5s like lock_control.py)
        extra = await self._collect(timeout=5.0)
        for f in extra:
            _LOGGER.debug(
                "Extra frame: cmd=0x%04x data=%s",
                f["cmd"],
                f.get("data", b"")[:40].hex(),
            )
            results.extend(self._extract_dps_from_frame(f))
        self._dispatch_dp_reports(frames + extra)
        return results

    async def _probe_collect(
        self, cmd: int, data: bytes, collect_seconds: float
    ) -> dict:
        """Send one frame and gather what comes back, raw frames and decoded DPs.

        Diagnostic helper for the history-read hunt. It keeps the raw frames
        next to the decoded DPs because every answer so far has been an ack
        that carries no DPs at all, which made the lock look silent when it
        was in fact replying.
        """
        frames = await self._send_recv(cmd, data, self._session_sec, wait=3.0)
        await self._handle_time_requests(frames)
        deadline = time.monotonic() + collect_seconds
        while time.monotonic() < deadline:
            extra = await self._collect(timeout=2.0)
            if not extra:
                continue
            await self._handle_time_requests(extra)
            frames.extend(extra)
        dps: list[dict] = []
        for f in frames:
            dps.extend(self._extract_dps_from_frame(f))
        return {
            "frames": [
                {"cmd": f["cmd"], "hex": f.get("data", b"").hex()} for f in frames
            ],
            "dps": dps,
        }

    async def async_probe_raw(
        self, dp_id: int, payload: bytes, collect_seconds: float = 6.0
    ) -> dict:
        """Write a RAW DP and return every frame the lock sends back.

        Unlike async_send_dp this does not filter on dp_id, so DP20/DP72
        record frames (or anything else the lock volunteers) survive.
        Never called during normal operation.
        """
        cmd, initial = self._build_dp_payload(dp_id, 0, payload)
        return await self._probe_collect(cmd, initial, collect_seconds)

    async def async_probe_status(
        self, payload: bytes, collect_seconds: float = 6.0
    ) -> dict:
        """Query CMD_DEVICE_STATUS with an explicit DP-id list.

        async_query_status sends an empty payload, and the lock answers with
        its default status set only -- the record DPs are never in it. Tuya's
        status query also takes a list of DP ids, so asking for the record DPs
        by name is worth a try. This is a read on a command the integration
        already sends every session, so it cannot change lock state.
        """
        return await self._probe_collect(
            CMD_DEVICE_STATUS, payload, collect_seconds
        )

    async def async_send_dp_raw_long(
        self, dp_id: int, payload: bytes, timeout: float = 60.0
    ) -> list[dict]:
        """Send a RAW DP and collect all DP reports over an extended period.

        Used for fingerprint/card enrollment where the device sends multiple
        progress reports over 30-60 seconds.
        """
        cmd, initial_payload = self._build_dp_payload(dp_id, 0, payload)
        frames = await self._send_recv(cmd, initial_payload, self._session_sec, wait=10.0)
        await self._handle_time_requests(frames)
        results: list[dict] = []
        for f in frames:
            results.extend(self._extract_dps_from_frame(f))

        # Wait for multi-step enrollment reports
        deadline = time.monotonic() + timeout
        done = False
        while not done and time.monotonic() < deadline:
            extra = await self._collect(timeout=5.0)
            for f in extra:
                for dp in self._extract_dps_from_frame(f):
                    results.append(dp)
                    if dp["id"] == dp_id and dp["type"] == 0 and len(dp["raw"]) >= 2:
                        stage = dp["raw"][1]
                        if stage in (0xFF, 0xFD, 0xFE):  # COMPLETE, FAILED, CANCELLED
                            done = True
        self._dispatch_dp_reports(frames)
        return results

    def _has_v5_activation_seed(self) -> bool:
        return bool(
            self._auth_key
            and self._auth_random
            and len(self._auth_random) == 16
            and self._local_key
            and self._sec_key
            and self._verify_key != b"\x00\x00\x00\x00"
        )

    async def _connect_for_pairing(self) -> bool:
        await self.async_disconnect()
        current_address = self._ble_device.address
        fresh = bluetooth.async_ble_device_from_address(
            self._hass, current_address, connectable=True
        )
        if fresh:
            self._ble_device = fresh
        details = getattr(self._ble_device, "details", None) or {}
        source = (
            details.get("source")
            if isinstance(details, dict)
            else getattr(details, "source", None)
        )
        _LOGGER.debug(
            "Pairing connection for %s (source=%s, rssi=%s): connecting...",
            self._ble_device.address,
            source,
            getattr(self._ble_device, "rssi", "?"),
        )
        self._client = await establish_connection(
            client_class=BleakClient,
            device=self._ble_device,
            name="tuya_ble_access",
            disconnected_callback=self._on_disconnect,
            max_attempts=2,
        )
        write_uuid, notify_uuid = self._resolve_gatt_uuids()
        if not write_uuid or not notify_uuid:
            _LOGGER.error("No compatible GATT characteristics for %s", self._ble_device.address)
            return False
        try:
            await self._client.stop_notify(notify_uuid)
            await asyncio.sleep(0.2)
        except Exception:
            pass
        self._notif_buf.clear()
        await self._client.start_notify(notify_uuid, self._on_notify)
        self.is_connected = True
        return True

    async def _request_v5_device_info(
        self, sec_flag: int, fixed_iv: bytes | None = None
    ) -> dict | None:
        self._notif_buf.clear()
        await self._send_encrypted(
            CMD_DEVICE_INFO,
            struct.pack(">H", 20),
            sec_flag,
            fixed_iv=fixed_iv,
        )
        await asyncio.sleep(4.0)
        raw = list(self._notif_buf)
        self._notif_buf.clear()
        if not raw:
            return None
        frames = parse_frames(self._keys, raw)
        for f in frames:
            if f["cmd"] == CMD_DEVICE_INFO:
                return _parse_v5_device_info(f["data"])
        return None

    async def _verify_v5_bound(self) -> bool:
        for attempt in range(3):
            try:
                if not await self._connect_for_pairing():
                    await asyncio.sleep(2.0)
                    continue
                info = await self._request_v5_device_info(SEC_NEW_SEC)
                if info:
                    _LOGGER.info("V5 bind verify sec=14 isBind=%s", info["is_bound"])
                    return bool(info["is_bound"])
            except Exception as exc:
                _LOGGER.debug("V5 bind verify attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(2.0)
        return False

    async def _get_activation_device_info(self):
        """Read device info before any PAIR command; safe to time out here."""
        info = None
        for attempt in range(5):
            _LOGGER.info("V5 pair attempt %d/5: connecting...", attempt + 1)
            try:
                if not await self._connect_for_pairing():
                    await asyncio.sleep(2.0)
                    continue
                info = await self._request_v5_device_info(
                    SEC_ENCRYPTED_AUTH_KEY,
                    fixed_iv=self._auth_random,
                )
                if info:
                    break
            except Exception as exc:
                _LOGGER.warning("V5 attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(2.0)

        if not info:
            raise PairingFailedError(
                "No V5 device info response; check Bluetooth reachability and competing connections."
            )
        return info

    async def _async_pair_first_activation_v5(self) -> tuple[bytes, bytes]:
        if not self._auth_key or not self._auth_random or not self._local_key or not self._sec_key:
            raise PairingFailedError("Missing V5 activation seed data")

        try:
            async with asyncio.timeout(45):
                info = await self._get_activation_device_info()
        except TimeoutError as exc:
            raise PairingFailedError(
                "Bluetooth device info timed out after 45 seconds; no PAIR command was sent."
            ) from exc
        if info["is_bound"]:
            raise DeviceAlreadyBoundError("Lock is already bound; factory reset required for first activation")

        srand = info["srand"]
        self._derive_session(srand)
        dev_id = self._virtual_id.rstrip(b"\x00").decode("ascii", errors="ignore")
        layouts = (
            [True, False]
            if info["support_struct_dp"] is None
            else [info["support_struct_dp"], not info["support_struct_dp"]]
        )

        pair_success = False
        for support_struct_dp in layouts:
            pair_data = _build_v5_pair_payload(
                local_key=self._local_key,
                sec_key=self._sec_key,
                dev_id=dev_id,
                dev_uuid=self._device_uuid,
                verify_key=self._verify_key,
                need_beacon=info["need_beacon"],
                support_struct_dp=support_struct_dp,
            )
            _LOGGER.info(
                "Trying V5 CMD_PAIR sec=12 structDp=%s payload_len=%d",
                support_struct_dp,
                len(pair_data),
            )
            pair_frames = await self._send_recv(CMD_PAIR, pair_data, SEC_NEW_PAIR, wait=8.0)
            await self._handle_time_requests(pair_frames)
            self._dispatch_dp_reports(pair_frames)
            for pf in pair_frames:
                status = pf["data"][0] if pf.get("data") else -1
                if pf["cmd"] == CMD_PAIR and status == 0x00:
                    pair_success = True
                    break
            if pair_success:
                break
            if not self._client or not self._client.is_connected:
                if await self._verify_v5_bound():
                    pair_success = True
                    break
                if not await self._connect_for_pairing():
                    break
                info = await self._request_v5_device_info(
                    SEC_ENCRYPTED_AUTH_KEY,
                    fixed_iv=self._auth_random,
                ) or info

        if not pair_success and not await self._verify_v5_bound():
            raise PairingFailedError(
                "V5 pairing failed; no successful response from device"
            )

        if not await self._verify_v5_bound():
            raise BindVerificationError(
                "V5 pairing completed but bind verification failed"
            )

        await self.async_disconnect()
        self._login_key = self._local_key[:6]
        self._virtual_id = (dev_id.encode("ascii") + b"\x00" * 22)[:22]
        return self._login_key, self._virtual_id

    async def async_pair_first_activation(
        self, auth_key_hex: str
    ) -> tuple[bytes, bytes]:
        """Perform first-time pairing with the lock.

        Closely follows lock_control.py connect_and_setup() first-activation path.
        Returns (login_key, virtual_id) bytes on success.
        """
        auth_key_bytes = bytes.fromhex(auth_key_hex) if auth_key_hex else b""
        if auth_key_bytes:
            self._auth_key = auth_key_bytes
            self._keys[SEC_AUTH_KEY] = auth_key_bytes
            self._keys[SEC_ENCRYPTED_AUTH_KEY] = auth_key_bytes
        if self._has_v5_activation_seed():
            return await self._async_pair_first_activation_v5()

        # Step 1: Device info with sec_flag=0 (unencrypted), with MTU
        # Retry with fresh BLE connection each attempt (device sleeps between ads)
        mtu_data = struct.pack(">H", 20)
        srand = None
        for attempt in range(5):
            _LOGGER.info("Pair attempt %d/5: connecting...", attempt + 1)
            try:
                await self.async_disconnect()
                self._client = await establish_connection(
                    client_class=BleakClient,
                    device=self._ble_device,
                    name="tuya_ble_access",
                    disconnected_callback=self._on_disconnect,
                    max_attempts=2,
                )
                # Log discovered services
                if self._client.services:
                    for svc in self._client.services:
                        chars = [
                            f"{c.uuid}({','.join(c.properties)})"
                            for c in svc.characteristics
                        ]
                        _LOGGER.info("  Service %s: %s", svc.uuid, chars)
                write_uuid, notify_uuid = self._resolve_gatt_uuids()
                if not write_uuid or not notify_uuid:
                    _LOGGER.error(
                        "No compatible GATT characteristics for %s",
                        self._ble_device.address,
                    )
                    await asyncio.sleep(2.0)
                    continue
                try:
                    await self._client.stop_notify(notify_uuid)
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
                await self._client.start_notify(notify_uuid, self._on_notify)
                self.is_connected = True

                # Send device info unencrypted — matches lock_control.py exactly
                _LOGGER.info("Connected, sending device info (sec_flag=0)")
                self._notif_buf.clear()
                await self._send_encrypted(CMD_DEVICE_INFO, mtu_data, SEC_NONE)
                await asyncio.sleep(4.0)  # flat 4s wait like lock_control.py

                raw = list(self._notif_buf)
                self._notif_buf.clear()
                if not raw:
                    _LOGGER.debug("No device info response on attempt %d", attempt + 1)
                    await asyncio.sleep(2.0)
                    continue

                _LOGGER.info("Got %d raw notifications for device info", len(raw))
                frames = parse_frames(self._keys, raw)
                if not frames:
                    # Try parsing as unencrypted manually
                    payloads = ble_protocol.reassemble(raw)
                    for p in payloads:
                        if p and p[0] == 0:
                            try:
                                f = ble_protocol.TuyaBleFrame.from_bytes(p[1:])
                                frames = [
                                    {
                                        "cmd": f.code,
                                        "sn": f.sn,
                                        "ack_sn": f.ack_sn,
                                        "data": f.data,
                                        "sec_flag": 0,
                                    }
                                ]
                            except Exception:
                                pass

                for f in frames:
                    if f["cmd"] == CMD_DEVICE_INFO and len(f["data"]) >= 12:
                        srand = f["data"][6:12]
                        bound = f["data"][5]
                        _LOGGER.info(
                            "Device bound=%s, srand=%s",
                            "YES" if bound else "NO",
                            srand.hex(),
                        )
                        break
                if srand:
                    break
            except Exception as exc:
                _LOGGER.warning("Attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(2.0)

        if not srand:
            raise PairingFailedError(
                "No device info response after 5 attempts — "
                "device may be already bound or not in pairing mode."
            )

        # Derive session keys
        if auth_key_bytes:
            self._auth_key = auth_key_bytes
            self._keys[SEC_AUTH_KEY] = auth_key_bytes
        self._derive_session(srand)

        # Step 2: Generate new login_key and virtual_id
        new_login_key = secrets.token_bytes(6)
        new_virtual_id = secrets.token_bytes(22)
        uuid_bytes = self._device_uuid.encode()[:16]
        pair_data = uuid_bytes + new_login_key + new_virtual_id
        pair_data = (pair_data + b"\x00" * 44)[:44]

        _LOGGER.info(
            "Pair data: uuid=%s login_key=%s virtual_id=%s",
            uuid_bytes.hex(),
            new_login_key.hex(),
            new_virtual_id.hex(),
        )

        # Build full trial key set (matches lock_control.py trial_keys)
        new_session_key = hashlib.md5(new_login_key + srand).digest()
        new_key4 = hashlib.md5(new_login_key).digest()
        self._keys[SEC_SESSION_KEY] = new_session_key
        self._keys[SEC_LOGIN_KEY] = new_key4
        # key 0 placeholder for unencrypted-response parsing (not used for AES)

        # Step 3: Try pairing — SEC_NONE first (correct for unbound/reset devices),
        # then encrypted variants as fallback
        pair_success = False
        for try_flag in [SEC_NONE, SEC_AUTH_SESSION, SEC_AUTH_KEY]:
            if try_flag != SEC_NONE and not self._keys.get(try_flag):
                _LOGGER.debug("Skipping sec_flag=%d (no key)", try_flag)
                continue

            # Reconnect if connection was lost during a previous attempt
            if not self._client or not self._client.is_connected:
                _LOGGER.info("Reconnecting before sec_flag=%d attempt...", try_flag)
                try:
                    await self.async_disconnect()
                    self._client = await establish_connection(
                        client_class=BleakClient,
                        device=self._ble_device,
                        name="tuya_ble_access",
                        disconnected_callback=self._on_disconnect,
                        max_attempts=2,
                    )
                    write_uuid, notify_uuid = self._resolve_gatt_uuids()
                    if not write_uuid or not notify_uuid:
                        _LOGGER.warning(
                            "No compatible GATT characteristics after reconnect"
                        )
                        continue
                    try:
                        await self._client.stop_notify(notify_uuid)
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
                    await self._client.start_notify(notify_uuid, self._on_notify)
                    self.is_connected = True
                except Exception as exc:
                    _LOGGER.warning("Reconnect failed: %s", exc)
                    continue

            _LOGGER.info("Trying CMD_PAIR with sec_flag=%d", try_flag)

            if try_flag == SEC_NONE:
                # Unencrypted pair — manual send + long wait (matches lock_control.py)
                try:
                    self._notif_buf.clear()
                    await self._send_encrypted(CMD_PAIR, pair_data, SEC_NONE)
                    await asyncio.sleep(5.0)  # flat 5s wait like lock_control.py
                    raw_resp = list(self._notif_buf)
                    self._notif_buf.clear()
                except Exception as exc:
                    _LOGGER.warning("SEC_NONE pair send failed: %s", exc)
                    continue

                if not raw_resp:
                    _LOGGER.info("No response for sec_flag=0 pair")
                    continue

                _LOGGER.info("Got %d notifications for sec_flag=0 pair", len(raw_resp))
                p_msgs = ble_protocol.reassemble(raw_resp)
                _LOGGER.info("Reassembled %d messages", len(p_msgs))

                # Device may respond encrypted with new session key (sec_flag=5)
                # or unencrypted (sec_flag=0). Try all keys like lock_control.py.
                trial_keys = dict(self._keys)
                for mi, p0 in enumerate(p_msgs):
                    sec_byte = p0[0] if p0 else -1
                    _LOGGER.info(
                        "  msg[%d]: sec_flag=%d len=%d hex=%s",
                        mi,
                        sec_byte,
                        len(p0),
                        p0[:40].hex(),
                    )
                    if sec_byte == 0:
                        # Unencrypted response
                        try:
                            f = ble_protocol.TuyaBleFrame.from_bytes(p0[1:])
                            _LOGGER.info("    cmd=0x%04X data=%s", f.code, f.data.hex())
                            if f.code == CMD_PAIR and f.data and f.data[0] == 0x00:
                                pair_success = True
                        except Exception as exc:
                            _LOGGER.debug("    Unencrypted parse failed: %s", exc)
                    else:
                        key = trial_keys.get(sec_byte)
                        if key:
                            try:
                                raw_dec = ble_protocol.decrypt_frame(key, p0)
                                f = ble_protocol.TuyaBleFrame.from_bytes(raw_dec)
                                _LOGGER.info(
                                    "    Decrypted: cmd=0x%04X data=%s",
                                    f.code,
                                    f.data.hex() if f.data else "",
                                )
                                if f.code == CMD_PAIR and f.data and f.data[0] == 0x00:
                                    pair_success = True
                            except Exception as exc:
                                _LOGGER.debug(
                                    "    Decrypt failed with key %d: %s", sec_byte, exc
                                )
                        else:
                            _LOGGER.info("    No key for sec_flag=%d", sec_byte)
                if pair_success:
                    break
            else:
                # Encrypted pair attempt
                try:
                    pair_frames = await self._send_recv(
                        CMD_PAIR, pair_data, try_flag, wait=8.0
                    )
                except Exception as exc:
                    _LOGGER.warning("Pair with sec_flag=%d failed: %s", try_flag, exc)
                    continue
                if pair_frames:
                    for pf in pair_frames:
                        status = pf["data"][0] if pf.get("data") else -1
                        _LOGGER.info(
                            "  Pair response: cmd=0x%04X status=%d", pf["cmd"], status
                        )
                        if pf["cmd"] == CMD_PAIR and status == 0x00:
                            pair_success = True
                            break
                    if pair_success:
                        await self._handle_time_requests(pair_frames)
                        break
                else:
                    _LOGGER.info("  No response for sec_flag=%d", try_flag)

        if not pair_success:
            raise PairingFailedError(
                "Pairing failed — no successful response from device"
            )

        _LOGGER.info("Pair SUCCESS! login_key=%s", new_login_key.hex())

        # Update keys for the new credentials
        self._login_key = new_login_key
        self._virtual_id = new_virtual_id
        self._keys[SEC_LOGIN_KEY] = hashlib.md5(new_login_key).digest()
        self._derive_session(srand)

        # Collect any extra notifications
        extra = await self._collect(timeout=2.0)
        await self._handle_time_requests(extra)
        await self.async_disconnect()

        return new_login_key, new_virtual_id
