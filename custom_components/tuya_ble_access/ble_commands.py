"""High-level DP payload builders and parsers for Tuya BLE locks."""

import struct
from typing import Any

from .const import (
    CRED_PASSWORD,
    CRED_CARD,
    CRED_FINGERPRINT,
    CRED_FACE,
    STAGE_START,
    STAGE_NAMES,
)

CRED_TYPE_NAMES = {
    CRED_PASSWORD: "password",
    CRED_CARD: "card",
    CRED_FINGERPRINT: "fingerprint",
    CRED_FACE: "face",
}

# DP 54 sync marker — required before biometric enrollment
SYNC_MARKER = b"\x03\x01\x02"


def parse_credential_list(raw: bytes) -> list[dict]:
    """Decode a DP54 credential-list response into credential records.

    Observed on the K3 BLE PRO 2 (2026-09-01) for a lock holding two enrolled
    fingerprints in slots 1 and 2::

        00 00 | 01 03 01 01 | 02 03 01 01
        hdr   | hw,type,mbr,f| hw,type,mbr,f

    Layout: a 2-byte header followed by 4-byte records
    ``[hw_id][cred_type][member_id][flag]``. An empty type answers with just a
    short summary frame (e.g. ``0100``) and yields no records.

    The lock also emits a short summary frame alongside the list; callers should
    feed every DP54 frame through this and keep whatever it returns.
    """
    if len(raw) < 6 or (len(raw) - 2) % 4 != 0:
        return []
    records = []
    for pos in range(2, len(raw), 4):
        hw_id, cred_type, member_id, flag = raw[pos:pos + 4]
        records.append({
            "hw_id": hw_id,
            "cred_type": cred_type,
            "member_id": member_id,
            "flag": flag,
        })
    return records


def parse_sync_bitmap(raw: bytes) -> list[int]:
    """Decode a DP54 credential-partition bitmap into occupied slot indices.

    The lock answers a DP54 sync query with [partition_id(1)][bitmap(1)] pairs;
    each partition holds 8 slots. A set bit means that slot holds a credential.
    Slot index (== hw_id) = (partition_id - 1) * 8 + bit_position.

    Ported verbatim from tools/lock_control.py so the integration can enumerate
    what the lock actually has enrolled (including credentials HA never stored).
    """
    if len(raw) < 2 or raw == b"\x00\x00":
        return []
    slots: list[int] = []
    pos = 0
    while pos + 1 < len(raw):
        part_id = raw[pos]
        bitmap = raw[pos + 1]
        pos += 2
        if part_id == 0 and bitmap == 0:
            continue
        for bit in range(8):
            if bitmap & (1 << bit):
                slots.append((part_id - 1) * 8 + bit)
    return slots


def build_validity_permanent() -> bytes:
    """17-byte validity block for permanent access.

    Permanent access: 2000-01-01 to 2030-12-31, no recurrence.
    """
    return (
        struct.pack(">I", 0x386CD300)   # start: 2000-01-01 00:00:00 UTC
        + struct.pack(">I", 0x72BC9B7F) # end:   2030-12-31 23:59:59 UTC
        + b"\x00"                        # pattern: no recurrence
        + b"\x00\x00\x00\x00"           # recurring bits: none
        + b"\x00\x00"                    # period start: 00:00
        + b"\x17\x3b"                    # period end: 23:59
    )


def build_enroll_payload(
    cred_type: int,
    member_id: int,
    admin: bool = False,
    password_digits: list[int] | None = None,
) -> bytes:
    """Build DP 1 (unlock_method_create) RAW payload.

    Format: type, stage, admin, member, hw_id, validity, times, pwd_len, pwd_data.
    """
    pwd = bytes(password_digits) if password_digits else b""
    payload = bytes([
        cred_type,
        STAGE_START,
        0x01 if admin else 0x00,
        member_id & 0xFF,
        0xFF,  # hardware ID: auto-assign
    ])
    payload += build_validity_permanent()
    payload += bytes([
        0x00,  # times: permanent
        len(pwd),
    ])
    payload += pwd
    return payload


def build_delete_payload(cred_type: int, member_id: int, hw_id: int) -> bytes:
    """Build DP 2 (unlock_method_delete) RAW payload.

    Adapted from lock_control.py do_delete_method (lines 826-835).
    """
    return bytes([
        cred_type,
        0x00,              # stage
        0x00,              # admin flag
        member_id & 0xFF,
        hw_id & 0xFF,
        0x01,              # deletion method: single
    ])


def build_temp_password_payload(
    password_digits: list[int],
    name: str,
    effective_ts: int,
    expiry_ts: int,
    schedule: bytes | None = None,
) -> bytes:
    """Build documented DP51: type, 17-byte validity, uses, PIN length, PIN.

    The name is HA metadata, not part of the device payload.
    https://developer.tuya.com/en/docs/iot/ble?id=K9ow3vcpn71ua
    """
    pwd = bytes(password_digits)
    if not 6 <= len(pwd) <= 255 or any(d > 9 for d in pwd):
        raise ValueError("PIN must contain 6 to 255 digits")
    recurrence = schedule if schedule is not None else bytes(9)
    if len(recurrence) != 9:
        raise ValueError("Recurrence must contain 9 bytes")
    validity = struct.pack(">II", effective_ts, expiry_ts) + recurrence
    return b"\x00" + validity + bytes([0, len(pwd)]) + pwd


def parse_temp_password_response(raw: bytes) -> dict:
    """Accept only DP51's two-byte hardware ID and status response."""
    if len(raw) != 2:
        raise ValueError("Unexpected temporary PIN response length")
    return {"hw_id": raw[0], "result_code": raw[1]}


def parse_enroll_response(raw: bytes) -> dict:
    """Parse DP 1 enrollment response from device.

    Format: [type:1][stage:1][admin:1][member:1][hw_id:1][count:1][result:1]
    Stages: 0x00=START, 0xFC=PROGRESS, 0xFF=DONE
    """
    if len(raw) < 7:
        return {"raw": raw.hex()}
    return {
        "type": CRED_TYPE_NAMES.get(raw[0], f"0x{raw[0]:02x}"),
        "stage": STAGE_NAMES.get(raw[1], f"0x{raw[1]:02x}"),
        "admin": bool(raw[2]),
        "member_id": raw[3],
        "hw_id": raw[4],
        "count": raw[5],
        "result_code": raw[6],
        "result": "OK" if raw[6] == 0x00 else f"err=0x{raw[6]:02x}",
    }


def parse_dp_value(dp_id: int, dp_type: int, raw: bytes) -> Any:
    """Interpret raw DP value based on known IDs/types."""
    if dp_type == 1:  # BOOL
        return bool(raw[0])
    if dp_type == 2:  # VALUE (4 bytes big-endian)
        if len(raw) >= 4:
            return int.from_bytes(raw[:4], "big")
    if dp_type == 4:  # ENUM
        return raw[0]
    return raw
