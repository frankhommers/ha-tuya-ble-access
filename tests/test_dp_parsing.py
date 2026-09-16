"""Offline tests for V4/V5 DP report parsing.

These validate the decoding path end-to-end without needing a real lock:
from raw BLE bytes, through parse_dp_report, through the coordinator's
state-map resolution, to the final ``state`` dict the UI reads from.

Run with:
    python3 -m pytest tuya_ble_access/tests/test_dp_parsing.py -v
or as a plain script:
    python3 tuya_ble_access/tests/test_dp_parsing.py
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

# Make `ble_protocol` and `device_profiles` importable without Home Assistant.
import importlib.util  # noqa: E402
import types  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _load(name: str):
    """Load a module from the integration package as if it were part of the
    package, without actually executing __init__.py (which imports HA)."""
    pkg = sys.modules.setdefault("_tblpkg", types.ModuleType("_tblpkg"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblpkg.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(
        fq, ROOT / f"{name}.py", submodule_search_locations=[str(ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    return mod


_ble_protocol = _load("ble_protocol")
parse_dp_report = _ble_protocol.parse_dp_report
parse_event_record = _ble_protocol.parse_event_record

_PROFILE_DIR = ROOT / "device_profiles"


# --- fixtures ---

def _build_report(dps: list[tuple[int, int, bytes]], dp_id_width: int = 1) -> bytes:
    """Build a synthetic DP report payload with the given framing width.

    Wire format (see ble_protocol.parse_dp_report):
        [sn:4][flags:1][0x80] then one or more
        [dp_id:width][type:1][len:2][val]
    """
    hdr = struct.pack(">IBB", 1, 0x00, 0x80)
    body = b""
    for dp_id, dp_type, val in dps:
        if dp_id_width == 1:
            body += struct.pack(">BBH", dp_id, dp_type, len(val)) + val
        else:
            body += struct.pack(">HBH", dp_id, dp_type, len(val)) + val
    return hdr + body


# Real CMD_DEVICE_STATUS response from a K3 BLE PRO 2 (captured via the
# Home Assistant debug log at 2026-04-14 17:11:34). Layout:
#   [sn:4][flags:1=0xf0][0x80][0x00 pad] then [dp:1][type:1][len:2][val]…
K3_QUERY_BUNDLE = (
    "00000000 f0 80 00"
    "08 02 0004 00000064"   # DP 8  battery% = 100
    "2f 01 0001 00"          # DP 47 motor_state = false
    "1c 04 0001 01"          # DP 28 language = 1 (english)
    "1f 04 0001 02"          # DP 31 volume = 2 (normal)
    "22 04 0001 00"          # DP 34 unlock_switch = 0 (single_unlock)
    "21 01 0001 01"          # DP 33 auto_lock = true
)

# Real DP71 unlock echo from the same session. Different header format:
#   [sn:4][0x80][0x00 pad] then [dp:1][type:1][len:2][val]
DP71_UNLOCK_ECHO = (
    "0000003a 80 00"
    "47 00 0013 0001ffff3335323733363836 01 69de185f 0000"
)


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# --- tests ---

def test_k3_multi_dp_bundle_extracts_all_settings():
    dps = parse_dp_report(_hex(K3_QUERY_BUNDLE))
    ids = {dp["id"]: dp for dp in dps}
    assert set(ids) == {8, 47, 28, 31, 34, 33}, ids.keys()

    # Spot-check: correct type & decoded value for each
    assert ids[8]["type"] == 2 and int.from_bytes(ids[8]["raw"], "big") == 100
    assert ids[47]["type"] == 1 and ids[47]["raw"] == b"\x00"  # motor idle
    assert ids[28]["type"] == 4 and ids[28]["raw"] == b"\x01"  # english
    assert ids[31]["type"] == 4 and ids[31]["raw"] == b"\x02"  # volume=normal
    assert ids[34]["type"] == 4 and ids[34]["raw"] == b"\x00"  # single_unlock
    assert ids[33]["type"] == 1 and ids[33]["raw"] == b"\x01"  # auto_lock on


def test_single_dp71_echo_still_decodes_after_parser_switch():
    dps = parse_dp_report(_hex(DP71_UNLOCK_ECHO))
    assert len(dps) == 1
    dp = dps[0]
    assert dp["id"] == 71
    assert dp["type"] == 0
    assert dp["len"] == 19
    assert dp["raw"].startswith(b"\x00\x01\xff\xff")
    assert b"35273686" in dp["raw"]  # check code visible in echo


def test_event_record_extracts_dp_and_timestamp():
    """cmd=0x8007 event record carrying DP19 (BLE unlock, user_id 1)
    captured from the user's log at 2026-04-14 17:11:32.867."""
    data = bytes.fromhex("00000000ec00000169de59211302000400000001")
    dps = parse_event_record(data)
    assert len(dps) == 1
    dp = dps[0]
    assert dp["id"] == 19        # ble_unlock
    assert dp["type"] == 2       # value
    assert dp["len"] == 4
    assert int.from_bytes(dp["raw"], "big") == 1   # user_id 1
    assert dp["event_ts"] == 0x69DE5921  # timestamp attached


def test_two_byte_dp_id_fallback_smart_lock3_style():
    # Smart Lock 3 (SYD8811) reports battery on DP 520 (>255) with a
    # real 2-byte dp id. 1-byte parse would claim dp=2 type=8 which we
    # reject in favour of the 2-byte parse that consumes the full frame.
    report = _build_report([(520, 2, b"\x00\x00\x00\x55")], dp_id_width=2)
    dps = parse_dp_report(report)
    assert len(dps) == 1
    assert dps[0]["id"] == 520
    assert int.from_bytes(dps[0]["raw"], "big") == 85


def test_integration_state_map_resolves_k3_bundle():
    """Feed the bundle through the same state_map the coordinator uses."""
    profile = json.loads((_PROFILE_DIR / "ba2qk177.json").read_text())
    state_map = profile["state_map"]

    def _parse_value(raw: bytes, kind: str):
        if kind == "int":
            return int.from_bytes(raw, "big") if raw else 0
        if kind == "bool":
            return bool(raw[0]) if raw else False
        if kind == "raw_byte":
            return raw[0] if raw else 0
        if kind == "enum_string":
            return raw.decode("ascii", errors="replace")
        return None

    state: dict = {}
    for dp in parse_dp_report(_hex(K3_QUERY_BUNDLE)):
        mapping = state_map.get(str(dp["id"]))
        if not mapping:
            continue
        state[mapping["key"]] = _parse_value(dp["raw"], mapping["parse"])

    assert state["battery_percent"] == 100
    assert state["language"] == 1
    assert state["volume"] == 2
    assert state["auto_lock"] is True
    assert state["unlock_switch"] == 0
    assert state["motor_state"] is False


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:
            failures.append((name, exc))
            print(f"ERROR {name}: {exc!r}")
    sys.exit(1 if failures else 0)
