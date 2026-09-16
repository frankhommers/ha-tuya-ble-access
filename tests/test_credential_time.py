"""Temporary PIN validity must not depend on the server's local timezone."""

import asyncio
import os
import struct
import time
import types
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest

from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services
from test_enroll_payloads import ble_commands
from tests.test_dp_parsing import _load

clock = _load("credential_time")


@pytest.mark.parametrize("local, utc", [
    ("2026-07-03T15:00:00", "2026-07-03T13:00:00Z"),
    ("2026-01-03T15:00:00", "2026-01-03T14:00:00Z"),
    ("2026-07-03T15:00:00+02:00", "2026-07-03T13:00:00Z"),
    ("2026-07-03T15:00:00Z", "2026-07-03T15:00:00Z"),
    ("2026-07-03T15:00:00-04:00", "2026-07-03T19:00:00Z"),
])
def test_local_and_explicit_times(local, utc):
    start, _ = clock.parse_credential_window(local, "2027-01-01T00:00:00Z", "Europe/Amsterdam")
    assert start == int(datetime.fromisoformat(utc).timestamp())


@pytest.mark.parametrize("server_zone", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_server_timezone_cannot_shift_validity(server_zone):
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = server_zone
        time.tzset()
        start, end = clock.parse_credential_window("2026-07-03T15:00:00", "2026-07-03T16:00:00", "Europe/Amsterdam")
        assert start == int(datetime.fromisoformat("2026-07-03T13:00:00Z").timestamp())
        assert end - start == 3600
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


@pytest.mark.parametrize("start, end, hours", [
    ("2026-03-29T01:30:00", "2026-03-29T03:30:00", 1),
    ("2026-10-25T01:30:00", "2026-10-25T03:30:00", 3),
    ("2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00", 1),
])
def test_periods_crossing_clock_changes(start, end, hours):
    effective, expiry = clock.parse_credential_window(start, end, "Europe/Amsterdam")
    assert expiry - effective == hours * 3600


@pytest.mark.parametrize("start, end, error", [
    ("2026-03-29T02:30:00", "2026-03-29T04:00:00", "nonexistent_time"),
    ("2026-10-25T01:30:00", "2026-10-25T02:30:00", "ambiguous_time"),
    ("nonsense", "2026-07-03T16:00:00", "invalid_time"),
    ("1969-01-01T00:00:00Z", "2026-07-03T16:00:00", "invalid_time"),
    ("2026-07-03T15:00:00", "2107-01-01T00:00:00Z", "invalid_time"),
    ("2026-07-03T15:00:00Z", "2026-07-03T16:00:00+02:00", "invalid_period"),
    ("2026-07-03T15:00:00Z", "2026-07-03T15:00:00Z", "invalid_period"),
])
def test_invalid_windows(start, end, error):
    with pytest.raises(clock.CredentialTimeError) as caught:
        clock.parse_credential_window(start, end, "Europe/Amsterdam")
    assert caught.value.translation_key == "temp_password_" + error


def test_service_sends_and_stores_same_utc_window(monkeypatch):
    async def run():
        handler, coord, store = await setup(monkeypatch)
        await handler(call("2026-07-03T15:00:00", "2026-07-03T16:00:00"))
        dp, payload = coord._session.async_send_dp_raw.await_args.args
        assert dp == 51
        expected = (int(datetime.fromisoformat("2026-07-03T13:00:00Z").timestamp()),
                    int(datetime.fromisoformat("2026-07-03T14:00:00Z").timestamp()))
        assert struct.unpack(">II", payload[1:9]) == expected
        saved = store.async_add_temp_password.await_args.kwargs
        assert (saved["effective"], saved["expiry"]) == expected
        coord._session.async_disconnect.assert_awaited_once()
    asyncio.run(run())


def test_invalid_window_is_localized_before_connecting(monkeypatch):
    async def run():
        handler, coord, store = await setup(monkeypatch)
        with pytest.raises(ServiceHomeAssistantError) as caught:
            await handler(call("2026-10-25T02:30:00", "2026-10-25T04:00:00"))
        assert caught.value.translation_key == "temp_password_ambiguous_time"
        coord._async_ensure_connected.assert_not_awaited()
        coord._session.async_send_dp_raw.assert_not_awaited()
        store.async_add_temp_password.assert_not_awaited()
    asyncio.run(run())


def call(start, end):
    return types.SimpleNamespace(data={"device_id": "MAC_A", "name": "Test", "pin_code": "123456",
                                       "effective_time": start, "expiry_time": end})


async def setup(monkeypatch):
    monkeypatch.setattr(services, "build_temp_password_payload", ble_commands.build_temp_password_payload)
    monkeypatch.setattr(services, "parse_temp_password_response", ble_commands.parse_temp_password_response)
    coord = types.SimpleNamespace(
        profile={"services": {"create_temp_password": {"dp": 51}}},
        _async_ensure_connected=AsyncMock(),
        _op_lock=asyncio.Lock(),
        _temp_password_lock=asyncio.Lock(),
        async_update_listeners=Mock(),
        _session=types.SimpleNamespace(async_send_dp_raw=AsyncMock(return_value={"id": 51, "type": 0, "raw": b"\x00\x00"}),
                                      async_disconnect=AsyncMock()),
    )
    entry = _entry()
    entry.runtime_data = types.SimpleNamespace(coordinators={"MAC_A": coord})
    hass = FakeServiceHass([entry])
    hass.config = types.SimpleNamespace(time_zone="Europe/Amsterdam")
    store = types.SimpleNamespace(async_add_temp_password=AsyncMock(return_value=types.SimpleNamespace(password_id="test-id", name="Test", hw_id=0)))
    hass.data = {"tuya_ble_access": {"credential_store": store}}
    await services.async_register_services(hass)
    return hass.services.registration("tuya_ble_access", "create_temp_password")[2], coord, store
