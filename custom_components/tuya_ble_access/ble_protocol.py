"""Tuya BLE CommRod protocol — framing, encryption & fragmentation.

Adapted from protocol.py and lock_control.py in the PoC.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from .ble_crypto import aes_cbc_decrypt, aes_cbc_encrypt, crc16_modbus_bytes

_LOGGER = logging.getLogger(__name__)

# Frame constants
FRAG_TYPE_DATA = 4  # version nibble value for V4 (0x40)


def encode_varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value > 0:
            byte |= 0x80
        result.append(byte)
        if value == 0:
            break
    return bytes(result)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a varint at offset. Returns (value, new_offset)."""
    value = 0
    shift = 0
    pos = offset
    while pos < len(data):
        b = data[pos]
        value |= (b & 0x7F) << shift
        pos += 1
        shift += 7
        if not (b & 0x80):
            break
    return value, pos


@dataclass
class TuyaBleFrame:
    sn: int = 0
    ack_sn: int = 0
    code: int = 0
    data: bytes = b""

    def to_bytes(self) -> bytes:
        header = struct.pack(">IIHH", self.sn, self.ack_sn, self.code, len(self.data))
        frame = header + self.data
        return frame + crc16_modbus_bytes(frame)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TuyaBleFrame":
        if len(raw) < 14:
            raise ValueError(f"Frame too short: {len(raw)} bytes")
        sn, ack_sn, code, data_len = struct.unpack(">IIHH", raw[:12])
        data = raw[12 : 12 + data_len]
        crc_received = raw[12 + data_len : 12 + data_len + 2]
        crc_expected = crc16_modbus_bytes(raw[: 12 + data_len])
        if crc_received != crc_expected:
            raise ValueError(
                f"CRC mismatch: got {crc_received.hex()}, expected {crc_expected.hex()}"
            )
        return cls(sn=sn, ack_sn=ack_sn, code=code, data=data)


def encrypt_frame(
    key: bytes, security_flag: int, plaintext: bytes, iv: bytes | None = None
) -> bytes:
    if security_flag == 0:
        return bytes([security_flag]) + plaintext
    iv, ct = aes_cbc_encrypt(key, plaintext, iv=iv)
    return bytes([security_flag]) + iv + ct


def decrypt_frame(key: bytes, encrypted: bytes) -> bytes:
    sec_flag = encrypted[0]
    if sec_flag == 0:
        return encrypted[1:]
    iv = encrypted[1:17]
    ct = encrypted[17:]
    return aes_cbc_decrypt(key, iv, ct)


def fragment(encrypted_payload: bytes, mtu: int = 20, protocol_version: int = 4) -> list[bytes]:
    total_len = len(encrypted_payload)
    fragments: list[bytes] = []
    offset = 0
    frag_idx = 0
    ver_byte = protocol_version << 4  # 0x30 for V3, 0x40 for V4

    while offset < total_len:
        header = encode_varint(frag_idx)
        if frag_idx == 0:
            header += encode_varint(total_len)
            header += bytes([ver_byte])
        payload_size = mtu - len(header)
        chunk = encrypted_payload[offset : offset + payload_size]
        fragments.append(header + chunk)
        offset += len(chunk)
        frag_idx += 1
    return fragments


def reassemble(raw_notifications: list[bytes]) -> list[bytes]:
    """Reassemble raw BLE notifications into complete message payloads.

    Handles interleaved fragment streams — some devices (e.g. H8 Pro on service
    1910) send fragments from multiple messages concurrently on the same
    characteristic.  Each stream starts with a seq=0 fragment containing the
    total payload length.  Continuation fragments (seq > 0) are routed to the
    correct stream by matching expected sequence numbers and remaining capacity.
    """
    if not raw_notifications:
        return []

    # Each stream: [expected_next_seq, total_len, buf]
    streams: list[list] = []  # [[expected_seq, total_len, bytearray], ...]

    for notif in raw_notifications:
        if not notif:
            continue
        seq, pos = decode_varint(notif, 0)
        if seq == 0:
            # Start of a new message stream
            total_len, pos = decode_varint(notif, pos)
            pos += 1  # skip version/type byte
            buf = bytearray(notif[pos:])
            streams.append([1, total_len, buf])
        else:
            # Continuation fragment — find the right stream
            _, data_pos = decode_varint(notif, 0)
            frag_data = notif[data_pos:]
            assigned = False
            for stream in streams:
                exp_seq, total_len, buf = stream
                if exp_seq == seq:
                    remaining = total_len - len(buf)
                    if remaining >= len(frag_data):
                        buf.extend(frag_data)
                        stream[0] = seq + 1  # advance expected_seq
                        assigned = True
                        break
            if not assigned:
                # Fallback: assign to first stream expecting this seq (ignore capacity)
                for stream in streams:
                    if stream[0] == seq:
                        stream[2].extend(frag_data)
                        stream[0] = seq + 1
                        assigned = True
                        break
            if not assigned:
                _LOGGER.debug(
                    "Orphan fragment seq=%d len=%d — no matching stream",
                    seq, len(frag_data),
                )

    results = []
    for _, total_len, buf in streams:
        results.append(bytes(buf[:total_len]) if total_len else bytes(buf))
    return results


class SequenceCounter:
    def __init__(self):
        self._value = 0

    def next(self) -> int:
        self._value += 1
        return self._value


def build_command(
    code: int,
    data: bytes,
    security_flag: int,
    key: bytes | None,
    seq: SequenceCounter,
    mtu: int = 20,
) -> list[bytes]:
    frame = TuyaBleFrame(sn=seq.next(), ack_sn=0, code=code, data=data)
    raw = frame.to_bytes()
    if security_flag == 0 or key is None:
        encrypted = encrypt_frame(b"", 0, raw)
    else:
        encrypted = encrypt_frame(key, security_flag, raw)
    return fragment(encrypted, mtu)


def parse_frames(keys: dict[int, bytes], raw_notifications: list[bytes]) -> list[dict]:
    """Reassemble raw BLE notifications, decrypt, and return parsed frames.

    Args:
        keys: Dict mapping security flag -> AES key.
        raw_notifications: Raw BLE notification payloads.

    Returns:
        List of dicts with keys: cmd, sn, ack_sn, data, sec_flag.
    """
    payloads = reassemble(raw_notifications)
    _LOGGER.debug(
        "parse_frames: %d notifications → %d payloads, sizes=%s",
        len(raw_notifications), len(payloads),
        [len(p) for p in payloads],
    )
    frames = []
    for payload in payloads:
        sec_flag = payload[0]
        if sec_flag == 0:
            raw = payload[1:]
        else:
            key = keys.get(sec_flag)
            if not key:
                _LOGGER.warning(
                    "Skipping frame with sec_flag=%d (no key). Available keys: %s. Payload[0:32]=%s",
                    sec_flag, list(keys.keys()), payload[:32].hex(),
                )
                continue
            try:
                raw = decrypt_frame(key, payload)
            except Exception as exc:
                _LOGGER.debug("Decrypt failed (sec_flag=%d): %s", sec_flag, exc)
                continue
        try:
            f = TuyaBleFrame.from_bytes(raw)
            frames.append({
                "cmd": f.code,
                "sn": f.sn,
                "ack_sn": f.ack_sn,
                "data": f.data,
                "sec_flag": sec_flag,
            })
        except Exception as exc:
            _LOGGER.debug("Frame parse failed (sec_flag=%d): %s", sec_flag, exc)
            continue
    return frames


def _parse_klv(klv: bytes, dp_id_width: int) -> list[dict]:
    dps: list[dict] = []
    pos = 0
    hdr = dp_id_width + 3  # dp_id + type + 2-byte len
    while pos + hdr <= len(klv):
        if dp_id_width == 2:
            dp_id = struct.unpack(">H", klv[pos : pos + 2])[0]
        else:
            dp_id = klv[pos]
        dp_type = klv[pos + dp_id_width]
        dp_len = struct.unpack(
            ">H", klv[pos + dp_id_width + 1 : pos + dp_id_width + 3]
        )[0]
        if pos + hdr + dp_len > len(klv):
            break
        val = klv[pos + hdr : pos + hdr + dp_len]
        dps.append({"id": dp_id, "type": dp_type, "len": dp_len, "raw": val})
        pos += hdr + dp_len
    return dps, pos


def parse_dp_report(data: bytes) -> list[dict]:
    """Parse V4 DP report payload.

    Framings observed on real devices:
      * Legacy V4 (Smart Lock 3): [sn:4][flags:1][0x80] + [dp:2][type:1][len:2][val]
      * btScyChannel V5 (K3 BLE PRO 2): [sn:4][flags:1][0x80][0x00] + [dp:1][type:1][len:2][val]

    We try every combination of (6- or 7-byte header) × (1- or 2-byte dp
    id) and return whichever consumes the most of the payload. This
    handles legacy devices reporting DPs > 255 as well as the V5 bundled
    status response that crams 7 DPs behind a pad byte.
    """
    if len(data) < 6:
        return []

    best_dps: list[dict] = []
    best_score = -1
    for header_len in (7, 6):
        if len(data) < header_len:
            continue
        klv = data[header_len:]
        for width in (1, 2):
            dps, consumed = _parse_klv(klv, width)
            if not dps:
                continue
            # Prefer (a) more bytes consumed, (b) more DPs on tie.
            score = consumed * 1000 + len(dps)
            if score > best_score:
                best_score = score
                best_dps = dps
    return best_dps


def parse_event_record(data: bytes) -> list[dict]:
    """Parse a cmd=0x8007 event-record frame.

    This is what the lock uses to notify the app of 'things that happened'
    with a timestamp attached — BLE/keypad unlock attempts, alarm_lock
    events (wrong_finger / wrong_password / low_battery / pry / etc.),
    doorbell presses, hijack / duress triggers. Unlike cmd=0x8006 which is
    a state snapshot, 0x8007 is an event log entry.

    Observed layout:
      [version:1][sn:4][b_type:1][flag:1][time_type:1][timestamp:4|13][dp:1][type:1][len:2][val]…

    The timestamp is a Unix epoch; we attach it to every extracted DP so
    downstream state handlers can tell 'this happened now' from 'this is
    the last-known value'.
    """
    if len(data) < 12:
        return []
    # Byte 7 is the time type (from the app's handleDpWithTimeData): 0x01 = 4
    # raw big-endian seconds, 0x00 = 13 ASCII digits of epoch milliseconds.
    # The K3 always sends 0x01; the ms form is handled so another firmware
    # cannot make us read digits as a timestamp and DP ids.
    time_type = data[7]
    if time_type == 0x00:
        if len(data) < 21:
            return []
        try:
            ts = int(data[8:21].decode("ascii")) // 1000
        except (UnicodeDecodeError, ValueError):
            ts = 0
        klv = data[21:]
    else:
        ts = int.from_bytes(data[8:12], "big")
        klv = data[12:]
    dps = []
    pos = 0
    while pos + 4 <= len(klv):
        dp_id = klv[pos]
        dp_type = klv[pos + 1]
        dp_len = struct.unpack(">H", klv[pos + 2:pos + 4])[0]
        if pos + 4 + dp_len > len(klv):
            break
        val = klv[pos + 4:pos + 4 + dp_len]
        dps.append({
            "id": dp_id, "type": dp_type, "len": dp_len,
            "raw": val, "event_ts": ts,
        })
        pos += 4 + dp_len
    return dps


def parse_dp_report_v3(data: bytes) -> list[dict]:
    """Parse V3 DP report (RECV_DP 0x8001): [dp_id(1)][type(1)][len(1)][val]..."""
    dps = []
    pos = 0
    while pos + 3 <= len(data):
        dp_id = data[pos]
        dp_type = data[pos + 1]
        dp_len = data[pos + 2]
        if pos + 3 + dp_len > len(data):
            break
        val = data[pos + 3 : pos + 3 + dp_len]
        dps.append({"id": dp_id, "type": dp_type, "len": dp_len, "raw": val})
        pos += 3 + dp_len
    return dps


def build_v4_dp(dp_id: int, dp_type: int, value: bytes) -> bytes:
    """Build V4 DP write payload: [version(1)][reserved(4)][dp_id(1)][type(1)][len(2)][value]."""
    header = b"\x00\x00\x00\x00\x00"
    return header + struct.pack(">BBH", dp_id & 0xFF, dp_type, len(value)) + value


def build_v3_dp(dp_id: int, dp_type: int, value: bytes) -> bytes:
    """Build V3 KLV payload: [dp_id(1)][type(1)][len(1)][value]."""
    return struct.pack(">BBB", dp_id & 0xFF, dp_type, len(value)) + value


# ---------- time sync (device pulls time via CMD_TIME_V1 / CMD_TIME_V2) ----------
def build_time_payload(cmd: int, *, now: float | None = None) -> bytes:
    """Build the reply body for the lock's time request, always in UTC.

    0x8011 (V1): 13-char ASCII epoch milliseconds + int16 zone in 1/100 hour.
    0x8012 (V2): YY MM DD hh mm ss wday (1 byte each) + int16 zone in 1/100 hour.

    The zone is fixed at 0 and V2 carries UTC wall-clock fields. The lock has no
    display, so local time buys nothing, and a fixed zone avoids the DST/
    ``time.timezone`` pitfalls entirely.
    """
    if now is None:
        now = time.time()
    if cmd == 0x8011:
        return str(int(now * 1000)).encode() + struct.pack(">h", 0)
    if cmd == 0x8012:
        t = time.gmtime(now)
        return struct.pack(
            ">BBBBBBBh",
            t.tm_year % 100,
            t.tm_mon,
            t.tm_mday,
            t.tm_hour,
            t.tm_min,
            t.tm_sec,
            t.tm_wday,
            0,
        )
    raise ValueError(f"not a time request cmd: 0x{cmd:04x}")


# ---------- DP report acknowledgement ----------
def parse_report_header(data: bytes) -> dict | None:
    """Header of a V4/V5 DP report or event record (cmd 0x8006 / 0x8007):
    [version:1][sn:4][b_type:1][flag:1]. Bit 7 of b_type clear means the
    device wants an acknowledgement (the app's DpsReportRep.needAck)."""
    if len(data) < 7:
        return None
    b_type = data[5]
    return {
        "version": data[0],
        "sn": int.from_bytes(data[1:5], "big"),
        "b_type": b_type,
        "flag": data[6],
        "need_ack": not (b_type & 0x80),
    }


def build_report_ack(data: bytes, status: int = 0) -> bytes | None:
    """Body of the acknowledgement the app sends back for a DP report that
    asks for one: the header echoed plus a status byte (0 = ok). Sent with
    the same cmd and ack_sn = the report frame's sn. Returns None when the
    report does not want an ack."""
    h = parse_report_header(data)
    if h is None or not h["need_ack"]:
        return None
    return (
        bytes([h["version"]])
        + h["sn"].to_bytes(4, "big")
        + bytes([h["b_type"], h["flag"], status & 0xFF])
    )
