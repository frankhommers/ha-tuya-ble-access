"""Pause must preserve expiry/identity and never claim unconfirmed success."""

import asyncio
import copy
import struct
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_credential_store import _fresh_store, _load

pause = _load("temp_password_pause")


async def fixture(monkeypatch):
    monkeypatch.setattr(pause.time, "time", lambda: 1200)
    store = _fresh_store()
    rec = await store.async_add_temp_password("A", "Guest", 1000, 2000, hw_id=0)
    session = types.SimpleNamespace(async_send_dp_raw=AsyncMock(
        return_value={"id": 53, "type": 0, "raw": b"\x00\x00"}))
    return store, rec, session


def test_pause_resume_preserves_policy_and_survives_reload(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        result = await pause.async_set_paused(store, session, "A", rec.password_id, 53, True)
        assert result["pause_state"] == "paused"
        payload = session.async_send_dp_raw.await_args.args[1]
        assert payload[:2] == b"\x00\x01"
        assert struct.unpack(">II", payload[2:10]) == (1000, 2000)
        assert payload[10:] == bytes.fromhex("02000000000000173b0000")
        restarted = _fresh_store()
        restarted._data = copy.deepcopy(store._data)
        row = restarted.get_temp_password_overview("A")["temporary_passwords"][0]
        assert row["pause_state"] == "paused"
        result = await pause.async_set_paused(restarted, session, "A", rec.password_id, 53, False)
        assert result["pause_state"] == "active"
        assert session.async_send_dp_raw.await_args.args[1] == b"\x00\x01" + struct.pack(">II", 1000, 2000) + bytes(11)
        assert len(restarted._data["temp_passwords"]) == 1
        assert "pin_code" not in result
    asyncio.run(run())


@pytest.mark.parametrize("response", [None, {}, {"id": 52, "type": 0, "raw": b"\0\0"},
    {"id": 53, "type": 0, "raw": b"\1\0"}, {"id": 53, "type": 0, "raw": b"\0"},
    {"id": 53, "type": 1, "raw": b"\0\0"}, {"id": 53, "type": 0, "raw": b"\0\1"}])
def test_uncertain_or_rejected_ack_stays_unknown(monkeypatch, response):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.async_send_dp_raw.return_value = response
        with pytest.raises(pause.TempPasswordPauseError):
            await pause.async_set_paused(store, session, "A", rec.password_id, 53, True)
        saved = store._data["temp_passwords"][rec.password_id]
        assert saved["pause_state"] == "unknown" and saved["requested_paused"] is True
    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
def test_failure_during_send_persists_unknown_before_io(monkeypatch, error):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        async def send(*_):
            assert store._data["temp_passwords"][rec.password_id]["pause_state"] == "unknown"
            raise error()
        session.async_send_dp_raw.side_effect = send
        with pytest.raises(error):
            await pause.async_set_paused(store, session, "A", rec.password_id, 53, False)
        assert store._data["temp_passwords"][rec.password_id]["pause_state"] == "unknown"
    asyncio.run(run())


@pytest.mark.parametrize("failure_at", [1, 2])
def test_disk_failure_never_leaves_false_certainty(monkeypatch, failure_at):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        count = 0
        async def save():
            nonlocal count
            count += 1
            if count == failure_at:
                raise OSError()
        store.async_save = save
        with pytest.raises(OSError):
            await pause.async_set_paused(store, session, "A", rec.password_id, 53, True)
        assert session.async_send_dp_raw.await_count == failure_at - 1
        assert store._data["temp_passwords"][rec.password_id]["pause_state"] == (
            "active" if failure_at == 1 else "unknown")
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["expired", "other_lock", "reused", "legacy", "duplicate"])
def test_invalid_identity_or_expiry_never_writes(monkeypatch, scenario):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        lock = "A"
        if scenario == "expired":
            monkeypatch.setattr(pause.time, "time", lambda: 2000)
        elif scenario == "other_lock":
            lock = "B"
        elif scenario == "reused":
            await store.async_add_temp_password("A", "Replacement", 1100, 2100, hw_id=0)
        elif scenario == "legacy":
            store._data["temp_passwords"][rec.password_id]["hw_id"] = None
        else:
            store._data["temp_passwords"]["duplicate"] = {**rec.__dict__, "password_id": "duplicate"}
        with pytest.raises(pause.TempPasswordPauseError):
            await pause.async_set_paused(store, session, lock, rec.password_id, 53, False)
        session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())


def test_reset_during_write_never_resurrects_record(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        async def send(*_):
            await store.async_report_factory_reset("A")
            return {"id": 53, "type": 0, "raw": b"\0\0"}
        session.async_send_dp_raw.side_effect = send
        with pytest.raises(pause.TempPasswordPauseError):
            await pause.async_set_paused(store, session, "A", rec.password_id, 53, True)
        assert store._data["temp_passwords"] == {}
    asyncio.run(run())


def test_pause_preserves_expired_cleanup_and_old_records_default_active(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        del store._data["temp_passwords"][rec.password_id]["pause_state"]
        del store._data["temp_passwords"][rec.password_id]["requested_paused"]
        assert store.get_temp_passwords_for_lock("A")[0].pause_state == "active"
        await pause.async_set_paused(store, session, "A", rec.password_id, 53, True)
        assert store.get_expired_temp_passwords("A", 1999) == []
        assert store.get_expired_temp_passwords("A", 2000)[0].password_id == rec.password_id
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["success", "unsupported", "connect_failure", "expired"])
def test_ha_service_capability_serialization_and_disconnect(monkeypatch, scenario):
    from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services

    async def run():
        store, _, session = await fixture(monkeypatch)
        rec = await store.async_add_temp_password("MAC_A", "Service test", 1000, 2000, hw_id=0)
        session.async_disconnect = AsyncMock()
        coord = types.SimpleNamespace(
            _profile={"services": {"pause_temp_password": {"dp": 53, "strategy": "no_weekdays"}}},
            _op_lock=asyncio.Lock(), _temp_password_lock=asyncio.Lock(),
            _async_ensure_connected=AsyncMock(), _session=session,
            async_update_listeners=Mock(),
        )
        entry = _entry()
        entry.runtime_data = types.SimpleNamespace(coordinators={"MAC_A": coord})
        hass = FakeServiceHass([entry])
        hass.data = {"tuya_ble_access": {"credential_store": store}}
        await services.async_register_services(hass)
        handler = hass.services.registration("tuya_ble_access", "pause_temp_password")[2]
        call = types.SimpleNamespace(data={"device_id": "MAC_A", "password_id": rec.password_id})
        if scenario == "unsupported":
            coord._profile = {"services": {"modify_temp_password": {"dp": 53}}}
        elif scenario == "expired":
            monkeypatch.setattr(pause.time, "time", lambda: 2000)
        elif scenario == "connect_failure":
            coord._async_ensure_connected.side_effect = TimeoutError()
        if scenario == "success":
            async def send(*_):
                assert coord._op_lock.locked() and coord._temp_password_lock.locked()
                return {"id": 53, "type": 0, "raw": b"\0\0"}
            session.async_send_dp_raw.side_effect = send
            assert (await handler(call))["pause_state"] == "paused"
            resume = hass.services.registration("tuya_ble_access", "resume_temp_password")[2]
            assert (await resume(call))["pause_state"] == "active"
            assert session.async_disconnect.await_count == 2
            assert not coord._op_lock.locked() and not coord._temp_password_lock.locked()
        else:
            with pytest.raises(TimeoutError if scenario == "connect_failure" else ServiceHomeAssistantError):
                await handler(call)
            session.async_send_dp_raw.assert_not_awaited()
            assert session.async_disconnect.await_count == (1 if scenario == "connect_failure" else 0)
    asyncio.run(run())
