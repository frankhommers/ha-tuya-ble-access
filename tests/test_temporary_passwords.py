"""Confirmed temporary PIN IDs must survive creation, storage and unlock reports."""

import asyncio
import importlib.util
import sys
import time
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_activation_entrypoints import ServiceHomeAssistantError
from test_credential_time import setup, call
from test_credential_store import _fresh_store, credential_store
from test_enroll_payloads import ble_commands
from tests.test_dp_parsing import ROOT


@pytest.mark.parametrize("response, key", [
    (None, "temp_password_no_response"),
    ({"id": 51, "type": 0, "raw": b"\x07\x01"}, "temp_password_rejected"),
    ({"id": 51, "type": 0, "raw": b"\xff\x02"}, "temp_password_rejected"),
    ({"id": 51, "type": 0, "raw": b"\x07\x03"}, "temp_password_rejected"),
    ({"id": 51, "type": 0, "raw": b"\x07\x08"}, "temp_password_rejected"),
    ({"id": 51, "type": 0, "raw": b"\xff\x00"}, "temp_password_unconfirmed"),
    ({"id": 51, "type": 0, "raw": b"\x07"}, "temp_password_unconfirmed"),
    ({"id": 51, "type": 0, "raw": b"\x07\x00\x00"}, "temp_password_unconfirmed"),
    ({"id": 51, "type": 2, "raw": b"\x07\x00"}, "temp_password_unconfirmed"),
    ({"id": 52, "type": 0, "raw": b"\x07\x00"}, "temp_password_unconfirmed"),
])
def test_creation_requires_explicit_success(monkeypatch, response, key):
    async def run():
        handler, coord, store = await setup(monkeypatch)
        coord._session.async_send_dp_raw.return_value = response
        with pytest.raises(ServiceHomeAssistantError) as error:
            await handler(call("2026-07-03T15:00:00", "2026-07-03T16:00:00"))
        assert error.value.translation_key == key
        store.async_add_temp_password.assert_not_awaited()
        coord._session.async_disconnect.assert_awaited_once()
    asyncio.run(run())


def test_creation_returns_confirmed_id_without_pin(monkeypatch):
    async def run():
        handler, coord, store = await setup(monkeypatch)
        result = await handler(call("2026-07-03T15:00:00", "2026-07-03T16:00:00"))
        assert result["password_id"] == "test-id"
        assert result["hw_id"] == 0
        assert "pin_code" not in result
        assert store.async_add_temp_password.await_args.kwargs["hw_id"] == 0
        coord.async_update_listeners.assert_called_once()
    asyncio.run(run())


def test_slot_reuse_old_records_and_other_locks(monkeypatch):
    async def run():
        store = _fresh_store()
        monkeypatch.setattr(credential_store.time, "time", lambda: 1000.5)
        old = await store.async_add_temp_password("A", "Guest", 1000, 2000, hw_id=0)
        await store.async_add_temp_password("B", "Other lock", 1000, 2000, hw_id=0)
        await store.async_add_temp_password("A", "Old unknown", 1000, 2000)
        assert store.resolve_temp_password("A", 0, 1001).password_id == old.password_id
        assert store.resolve_temp_password("A", 1, 1001) is None
        assert store.resolve_temp_password("A", 0, 999) is None
        monkeypatch.setattr(credential_store.time, "time", lambda: 1500.5)
        new = await store.async_add_temp_password("A", "Cleaner", 1500, 2500, hw_id=0)
        assert store.resolve_temp_password("A", 0, 1400).password_id == old.password_id
        assert store.resolve_temp_password("A", 0, 1600).password_id == new.password_id
        # Same-second reuse is ambiguous at the lock's timestamp precision.
        assert store.resolve_temp_password("A", 0, 1500) is None
        assert store.resolve_temp_password("B", 0, 1600).name == "Other lock"
        reloaded = _fresh_store()
        reloaded._data = store._data
        assert reloaded.resolve_temp_password("A", 0, 1600).password_id == new.password_id
        await store.async_report_factory_reset("A")
        assert store.resolve_temp_password("A", 0, 1600) is None
        assert store.resolve_temp_password("B", 0, 1600).name == "Other lock"
    asyncio.run(run())


def test_failed_save_keeps_previous_mapping(monkeypatch):
    async def run():
        store = _fresh_store()
        monkeypatch.setattr(credential_store.time, "time", lambda: 1000)
        await store.async_add_temp_password("A", "Guest", 1000, 2000, hw_id=7)
        monkeypatch.setattr(credential_store.time, "time", lambda: 1500)
        store.async_save = AsyncMock(side_effect=OSError("disk"))
        with pytest.raises(OSError):
            await store.async_add_temp_password("A", "Cleaner", 1500, 2500, hw_id=7)
        assert store.resolve_temp_password("A", 7, 1600).name == "Guest"
    asyncio.run(run())


def coordinator(monkeypatch, store):
    # Import the real coordinator with minimal HA infrastructure stubs.
    modules = {
        "homeassistant.const": {"EVENT_HOMEASSISTANT_STOP": "stop"},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.helpers.update_coordinator": {"DataUpdateCoordinator": object, "UpdateFailed": Exception},
    }
    for name, attrs in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    package = types.ModuleType("_temporary_coordinator")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    spec = importlib.util.spec_from_file_location(package.__name__ + ".coordinator", ROOT / "coordinator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = module.TuyaBLELockCoordinator
    c = cls.__new__(cls)
    c._profile = {"state_map": {"55": {"key": "unlock_temporary", "parse": "int"},
                               "47": {"key": "motor_state", "parse": "bool"}}}
    c.state = {"motor_state": False}
    c._mac, c._device_name = "A", "Lock"
    c._entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(credential_store=store))
    c.hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_fire=Mock()))
    c._recent_event_keys = {}
    c._motor_unlock_at = 0
    c._motor_unlock_claimed = True
    c._last_unlock_committed_ts = 0
    c.async_set_updated_data = Mock()
    return c


@pytest.mark.parametrize("motor", [False, True])
def test_real_unlock_report_resolves_and_fires_once(monkeypatch, motor):
    async def run():
        store = _fresh_store()
        rec = await store.async_add_temp_password("A", "Weekendgast", 0, 0xFFFFFFFF, hw_id=0)
        c = coordinator(monkeypatch, store)
        if motor:
            c._process_dp_reports([{"id": 47, "type": 1, "raw": b"\x01"}])
        dp = {"id": 55, "type": 2, "raw": bytes(4), "event_ts": int(time.time())}
        c._process_dp_reports([dp, dp])
        events = [args for args, _ in c.hass.bus.async_fire.call_args_list
                  if args[0] == "tuya_ble_access_temporary_code_used"]
        assert len(events) == 1
        assert events[0][1]["credential"] == "Weekendgast"
        assert events[0][1]["password_id"] == rec.password_id
        assert events[0][1]["attributed"] is True
        assert c.state["last_unlock_credential"] == "Weekendgast"
        assert c.state["last_unlock_by"] == "Weekendgast"
        assert c.state["last_unlock_person"] is None
        assert c.state["recent_unlocks"][0]["password_id"] == rec.password_id
    asyncio.run(run())


def test_unknown_snapshot_and_backlog_do_not_invent_guest(monkeypatch):
    c = coordinator(monkeypatch, _fresh_store())
    dp = {"id": 55, "type": 2, "raw": b"\x00\x00\x00\x07"}
    c._process_dp_reports([dp])
    c.hass.bus.async_fire.assert_not_called()
    c._process_dp_reports([{**dp, "event_ts": int(time.time()) - 600}])
    c.hass.bus.async_fire.assert_not_called()
    assert c.state["recent_unlocks"][0]["password_id"] is None
    c._process_dp_reports([{**dp, "event_ts": int(time.time())}])
    event = [args for args, _ in c.hass.bus.async_fire.call_args_list
             if args[0] == "tuya_ble_access_temporary_code_used"][0][1]
    assert event["attributed"] is False
    assert event["credential"] is None
    assert event["password_id"] is None


def test_restored_zero_slot_does_not_fire_again(monkeypatch):
    c = coordinator(monkeypatch, _fresh_store())
    now = int(time.time())
    c.seed_unlock_baseline(now, 55, 0)
    c._process_dp_reports([{"id": 55, "type": 2, "raw": bytes(4), "event_ts": now}])
    c.hass.bus.async_fire.assert_not_called()


def test_motor_time_prevents_stale_clock_selecting_previous_guest(monkeypatch):
    async def run():
        store = _fresh_store()
        now = int(time.time())
        old = await store.async_add_temp_password("A", "Previous guest", 0, 0xFFFFFFFF, hw_id=7)
        store._data["temp_passwords"][old.password_id]["created_at"] = now - 90000
        new = await store.async_add_temp_password("A", "Current guest", 0, 0xFFFFFFFF, hw_id=7)
        c = coordinator(monkeypatch, store)
        c._process_dp_reports([{"id": 47, "type": 1, "raw": b"\x01"}])
        c._process_dp_reports([{"id": 55, "type": 2, "raw": b"\x00\x00\x00\x07", "event_ts": now - 72000}])
        # Creation and first use in the same second is deliberately ambiguous
        # if the ID was reused in that second; advance the generation boundary.
        assert c.state["last_unlock_credential"] is None
        store._data["temp_passwords"][old.password_id]["superseded_at"] = now - 10
        store._data["temp_passwords"][new.password_id]["created_at"] = now - 10
        c._process_dp_reports([{"id": 47, "type": 1, "raw": b"\x00"},
                               {"id": 47, "type": 1, "raw": b"\x01"}])
        c._process_dp_reports([{"id": 55, "type": 2, "raw": b"\x00\x00\x00\x07", "event_ts": now - 72000}])
        assert c.state["last_unlock_credential"] == "Current guest"
    asyncio.run(run())
