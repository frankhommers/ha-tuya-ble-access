"""DP3 schedule builders; individual pause/restore verified on ba2qk177."""

import re
import struct


NO_WEEKDAYS = bytes.fromhex("02000000000000173b")


def policy_from_enrollment(payload, dp_id, response):
    """Record only exact confirmed device identity, never PIN/biometric data."""
    if not isinstance(response, dict) or response.get("id") != dp_id or response.get("type") != 0:
        return None
    raw = response.get("raw")
    if not isinstance(raw, bytes) or len(raw) < 7 or len(payload) < 24:
        return None
    if (payload[0] not in (1, 2, 3) or payload[1] != 0 or payload[2] not in (0, 1)
            or not 1 <= payload[3] <= 100 or payload[4] != 255
            or raw[:4] != bytes([payload[0], 255, payload[2], payload[3]])
            or raw[4] == 255 or raw[6] != 0 or payload[22] != 0):
        return None
    return {"source": "ha_enrollment", "cred_type": raw[0], "member_id": raw[3],
            "hw_id": raw[4], "admin": bool(raw[2]),
            "validity_hex": payload[5:22].hex(), "uses": payload[22]}


def build_schedule_probe(policy, *, paused, scope="credential", pin_code=None):
    """Build a candidate DP3 write with an exact restore policy supplied.

Member policy must come from a separately recorded member schedule, never
from one credential's schedule. Ordinary PIN edits require the existing PIN;
zero-length PIN probes were rejected by the tested firmware. The low-level
builder still permits them for policy inspection and historical research.
"""
    if type(paused) is not bool or scope not in ("credential", "member"):
        raise ValueError("Invalid schedule operation")
    if pin_code is not None and (
        scope != "credential" or policy.get("cred_type") != 1
        or not isinstance(pin_code, str) or not re.fullmatch(r"[0-9]{6,10}", pin_code)
    ):
        raise ValueError("PIN content requires an ordinary PIN and 6-10 ASCII digits")
    member = policy.get("member_id")
    if type(member) is not int or not 1 <= member <= 100:
        raise ValueError("Unknown device member")
    if type(policy.get("admin")) is not bool or policy["admin"]:
        raise ValueError("Only dedicated non-admin test access is supported")
    if type(policy.get("uses")) is not int or policy["uses"] != 0:
        raise ValueError("Limited-use access cannot be restored safely")
    validity = bytes.fromhex(policy.get("validity_hex", ""))
    if len(validity) != 17 or struct.unpack(">I", validity[:4])[0] >= struct.unpack(">I", validity[4:8])[0]:
        raise ValueError("Original validity is required")
    if paused:
        validity = validity[:8] + NO_WEEKDAYS
    if scope == "member":
        if policy.get("source") != "confirmed_member_schedule":
            raise ValueError("A credential schedule is not a member schedule")
        # Product panel module 1051 uses three trailing zero bytes.
        return bytes([0, 0, 0, member, 255]) + validity + bytes(3)
    kind, slot = policy.get("cred_type"), policy.get("hw_id")
    if (policy.get("source") != "ha_enrollment" or type(kind) is not int
            or kind not in (1, 2, 3) or type(slot) is not int or not 0 <= slot <= 254):
        raise ValueError("A confirmed credential identity is required")
    digits = bytes(int(digit) for digit in pin_code) if pin_code is not None else b""
    return bytes([kind, 0, 0, member, slot]) + validity + bytes([0, len(digits)]) + digits


def schedule_probe_succeeded(response, payload):
    """DP3 success is ff, not the 00 success used by temporary PIN DP53."""
    if not isinstance(response, dict) or response.get("id") != 3 or response.get("type") != 0:
        return False
    raw = response.get("raw")
    return (isinstance(raw, bytes) and len(raw) == 7
            and raw[:5] == payload[:5] and raw[5:] == b"\x00\xff")
