"""Tests for credential enrolment payloads (DP1) and temp passwords (DP51).

The fingerprint path is proven against a real K3 BLE PRO 2 (two fingers
enrolled, hardware IDs 1 and 2). The PIN and card paths share the same builder
but have never been executed against the device, so these tests pin down the
wire format they currently produce — both as regression protection and as
documentation of what we will be comparing against when we do run them.

Note the digit encoding: digits go out as raw values (1 -> 0x01), NOT as ASCII
(1 -> 0x31). That is consistent with build_temp_password_payload, but is not
independently confirmed against the lock yet.

Run with:
    PYTHONPATH=. pytest tests/test_enroll_payloads.py -v
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _load(name: str):
    pkg = sys.modules.setdefault("_tblpkg", types.ModuleType("_tblpkg"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblpkg.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(fq, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fq] = module
    spec.loader.exec_module(module)
    return module


ble_commands = _load("ble_commands")
const = _load("const")

# Offsets into the DP1 payload.
OFF_TYPE, OFF_STAGE, OFF_ADMIN, OFF_MEMBER, OFF_HWID = 0, 1, 2, 3, 4
OFF_VALIDITY = 5
VALIDITY_LEN = 17
OFF_TIMES = OFF_VALIDITY + VALIDITY_LEN      # 22
OFF_PWD_LEN = OFF_TIMES + 1                  # 23
OFF_PWD = OFF_PWD_LEN + 1                    # 24
HEADER_LEN = OFF_PWD                         # 24 bytes before any digits


class TestValidityBlock:
    def test_is_seventeen_bytes(self):
        assert len(ble_commands.build_validity_permanent()) == VALIDITY_LEN

    def test_spans_2000_to_2030(self):
        block = ble_commands.build_validity_permanent()
        start, end = struct.unpack(">II", block[:8])
        assert start == 0x386CD300
        assert end == 0x72BC9B7F
        assert start < end

    def test_has_no_recurrence(self):
        block = ble_commands.build_validity_permanent()
        assert block[8] == 0x00              # pattern: none
        assert block[9:13] == b"\x00" * 4    # recurring bits: none


class TestFingerprintPayload:
    """This path is confirmed working against the real lock."""

    def _payload(self, **kw):
        return ble_commands.build_enroll_payload(const.CRED_FINGERPRINT, 2, **kw)

    def test_length_has_no_password_tail(self):
        assert len(self._payload()) == HEADER_LEN

    def test_header_fields(self):
        p = self._payload()
        assert p[OFF_TYPE] == const.CRED_FINGERPRINT
        assert p[OFF_STAGE] == const.STAGE_START
        assert p[OFF_ADMIN] == 0x00
        assert p[OFF_MEMBER] == 2
        assert p[OFF_HWID] == 0xFF           # let the lock assign the slot
        assert p[OFF_PWD_LEN] == 0

    def test_admin_flag_is_set(self):
        assert self._payload(admin=True)[OFF_ADMIN] == 0x01


class TestPinPayload:
    """Never executed against the device — format captured here on purpose."""

    def _payload(self, pin="123456", **kw):
        return ble_commands.build_enroll_payload(
            const.CRED_PASSWORD, 3, password_digits=[int(d) for d in pin], **kw
        )

    def test_length_grows_with_digit_count(self):
        assert len(self._payload("123456")) == HEADER_LEN + 6
        assert len(self._payload("1234567890")) == HEADER_LEN + 10

    def test_declared_length_matches_actual_digits(self):
        p = self._payload("1234567")
        assert p[OFF_PWD_LEN] == 7
        assert len(p) - OFF_PWD == p[OFF_PWD_LEN]

    def test_digits_are_raw_values_not_ascii(self):
        p = self._payload("123456")
        assert p[OFF_PWD:] == bytes([1, 2, 3, 4, 5, 6])
        assert p[OFF_PWD:] != b"123456"

    def test_zero_digit_survives(self):
        """A leading zero must not be truncated or shifted."""
        p = self._payload("012345")
        assert p[OFF_PWD:] == bytes([0, 1, 2, 3, 4, 5])

    def test_credential_type_is_password(self):
        assert self._payload()[OFF_TYPE] == const.CRED_PASSWORD

    def test_shares_header_layout_with_fingerprint(self):
        pin = self._payload()
        finger = ble_commands.build_enroll_payload(const.CRED_FINGERPRINT, 3)
        # Everything except the credential type and password tail must match.
        assert pin[OFF_STAGE:OFF_PWD_LEN] == finger[OFF_STAGE:OFF_PWD_LEN]


class TestMemberIdBounds:
    def test_member_id_is_truncated_to_one_byte(self):
        p = ble_commands.build_enroll_payload(const.CRED_PASSWORD, 0x1FF)
        assert p[OFF_MEMBER] == 0xFF


class TestTempPasswordPayload:
    def test_layout(self):
        payload = ble_commands.build_temp_password_payload(
            [1, 2, 3, 4, 5, 6], "Gast", 1000, 2000
        )
        # Tuya DP51: type + validity(17) + use count + length + digits.
        assert payload == bytes.fromhex(
            "00" "000003e8" "000007d0" "000000000000000000" "00" "06" "010203040506"
        )
        assert b"Gast" not in payload

    def test_uses_same_raw_digit_encoding_as_enrolment(self):
        payload = ble_commands.build_temp_password_payload([9, 8, 7, 6, 5, 4], "x", 0, 0)
        assert payload[20:] == bytes([9, 8, 7, 6, 5, 4])


class TestParseEnrollResponse:
    def test_success(self):
        raw = bytes([const.CRED_PASSWORD, 0xFF, 0x00, 0x03, 0x07, 0x01, 0x00])
        resp = ble_commands.parse_enroll_response(raw)
        assert resp["stage"] == "COMPLETE"
        assert resp["result"] == "OK"
        assert resp["hw_id"] == 0x07
        assert resp["member_id"] == 0x03
        assert resp["type"] == "password"

    def test_failure_reports_error_code(self):
        raw = bytes([const.CRED_PASSWORD, 0xFD, 0x00, 0x03, 0x00, 0x00, 0x05])
        resp = ble_commands.parse_enroll_response(raw)
        assert resp["stage"] == "FAILED"
        assert resp["result"] == "err=0x05"

    def test_short_response_has_no_stage(self):
        """Callers gate on stage == COMPLETE, so a truncated frame must
        never look like success."""
        resp = ble_commands.parse_enroll_response(b"\x01\xff")
        assert "stage" not in resp
        assert resp["result"] if "result" in resp else True

    def test_already_bound_card_preserves_status_and_unassigned_slot(self):
        # S25 Ultra enrollment reported by the user: the lock speaks success
        # but DP1 returns FAILED / ALREADY_BOUND_CARD, without a hardware slot.
        resp = ble_commands.parse_enroll_response(bytes.fromhex("02fd0001ffff08"))
        assert resp["type"] == "card"
        assert resp["stage"] == "FAILED"
        assert resp["result_code"] == 0x08
        assert resp["result"] == "err=0x08"
        assert resp["hw_id"] == 255
