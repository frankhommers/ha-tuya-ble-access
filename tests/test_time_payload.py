"""The lock pulls time from us on every connect; we always answer in UTC."""
import struct

from tests.test_dp_parsing import _load

build_time_payload = _load("ble_protocol").build_time_payload

# 2026-07-03 10:26:40 UTC (a Friday, tm_wday == 4)
_NOW = 1783074400.5


def test_v1_is_ascii_epoch_ms_plus_zero_zone():
    payload = build_time_payload(0x8011, now=_NOW)
    assert payload[:13] == b"1783074400500"
    assert struct.unpack(">h", payload[13:])[0] == 0


def test_v2_packs_utc_fields_and_zero_zone():
    payload = build_time_payload(0x8012, now=_NOW)
    assert payload == struct.pack(">BBBBBBBh", 26, 7, 3, 10, 26, 40, 4, 0)


def test_unknown_cmd_rejected():
    try:
        build_time_payload(0x0001)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---------- DP report ack ----------
_proto = _load("ble_protocol")

# Real K3 frames from the HA log.
_EVENT_0x8007 = bytes.fromhex("00000019390000016a9755e20c02000400000001")
_SNAPSHOT_0x8006 = bytes.fromhex("0000001938800008020004000000" "4f")


def test_event_record_wants_ack_and_ack_echoes_header():
    h = _proto.parse_report_header(_EVENT_0x8007)
    assert h == {"version": 0, "sn": 0x1939, "b_type": 0, "flag": 0, "need_ack": True}
    assert _proto.build_report_ack(_EVENT_0x8007) == bytes.fromhex("00" "00001939" "00" "00" "00")


def test_snapshot_with_bit7_set_wants_no_ack():
    assert _proto.parse_report_header(_SNAPSHOT_0x8006)["need_ack"] is False
    assert _proto.build_report_ack(_SNAPSHOT_0x8006) is None


def test_short_payload_is_not_acked():
    assert _proto.build_report_ack(b"\x00\x01") is None


# ---------- event record time types ----------
def test_event_record_with_ms_string_time_type():
    parse_event_record = _proto.parse_event_record
    hdr = bytes.fromhex("00" "00001939" "00" "00")
    rec = hdr + b"\x00" + b"1788302818123" + bytes.fromhex("0c02000400000001")
    dps = parse_event_record(rec)
    assert [(d["id"], d["raw"], d["event_ts"]) for d in dps] == [(12, b"\x00\x00\x00\x01", 1788302818)]


def test_event_record_with_seconds_time_type_unchanged():
    dps = _proto.parse_event_record(_EVENT_0x8007)
    assert [(d["id"], d["event_ts"]) for d in dps] == [(12, 0x6A9755E2)]
