"""Tests for DP54 credential-partition bitmap parsing (occupied lock slots)."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _load(name: str):
    pkg = sys.modules.setdefault("_tblbm", types.ModuleType("_tblbm"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblbm.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(fq, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fq] = module
    spec.loader.exec_module(module)
    return module


_load("const")
ble_commands = _load("ble_commands")
parse = ble_commands.parse_sync_bitmap


def test_empty_bitmap_is_no_slots():
    assert parse(b"") == []
    assert parse(b"\x00\x00") == []


def test_single_partition_low_bits():
    # partition 1, bits 0 and 2 set -> slots 0 and 2
    assert parse(bytes([0x01, 0b00000101])) == [0, 2]


def test_second_partition_offsets_by_eight():
    # partition 2, bit 1 set -> slot (2-1)*8 + 1 = 9
    assert parse(bytes([0x02, 0b00000010])) == [9]


def test_multiple_partitions_concatenated():
    # p1 bit0 (slot 0), p2 bit0 (slot 8); a zero-zero pair is skipped
    raw = bytes([0x01, 0b00000001, 0x00, 0x00, 0x02, 0b00000001])
    assert parse(raw) == [0, 8]


# ---- DP54 credential list (real captured bytes) ----------------------------

parse_list = ble_commands.parse_credential_list


def test_real_two_fingerprint_response_from_lock():
    """Captured 2026-09-01 from DC:23:51:D1:85:77 holding two fingerprints."""
    raw = bytes.fromhex("00000103010102030101")
    assert parse_list(raw) == [
        {"hw_id": 1, "cred_type": 3, "member_id": 1, "flag": 1},
        {"hw_id": 2, "cred_type": 3, "member_id": 1, "flag": 1},
    ]


def test_summary_frames_yield_no_records():
    # short frames the lock sends alongside the list / for empty types
    assert parse_list(bytes.fromhex("0101")) == []
    assert parse_list(bytes.fromhex("0100")) == []
    assert parse_list(b"") == []


def test_partial_trailing_bytes_rejected():
    assert parse_list(bytes.fromhex("000001030101ff")) == []
