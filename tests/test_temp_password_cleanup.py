"""Expired PIN cleanup: confirmed deletion, safe retries and historical names."""

import asyncio
import copy
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_credential_store import _fresh_store, credential_store
from test_temporary_passwords import coordinator
from tests.test_dp_parsing import _load

cleanup = _load("temp_password_cleanup")


async def fixture(monkeypatch, response=b"\x00\x00"):
    monkeypatch.setattr(credential_store.time, "time", lambda: 1000)
    store = _fresh_store()
    rec = await store.async_add_temp_password("A", "Guest", 1000, 2000, hw_id=0)
    monkeypatch.setattr(credential_store.time, "time", lambda: 2000)
    session = types.SimpleNamespace(is_connected=True, async_send_dp_raw=AsyncMock(
        return_value={"id": 52, "type": 0, "raw": response}))
    return store, rec, session


@pytest.mark.parametrize("status", [0, 2])
def test_confirmed_deletion_archives_at_expiry_and_keeps_history(monkeypatch, status):
    async def run():
        store, rec, session = await fixture(monkeypatch, bytes([0, status]))
        assert store.get_temp_password_overview("A")["temporary_passwords"] == []
        assert len(store.get_temp_password_overview("A")["expired_temporary_passwords"]) == 1
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 1
        session.async_send_dp_raw.assert_awaited_once_with(52, b"\x00")
        assert store.get_temp_password_overview("A") == {
            "temporary_passwords": [], "expired_temporary_passwords": [],
            "archived_temporary_password_count": 1}
        assert store.resolve_temp_password("A", 0, 1500).password_id == rec.password_id
        assert store.resolve_temp_password("A", 0, 2001) is None
        # Restart persistence: no second deletion, but old names remain resolvable.
        restored = _fresh_store()
        restored._data = copy.deepcopy(store._data)
        assert await cleanup.async_cleanup_expired_passwords(restored, session, "A", 52) == 0
        assert restored.resolve_temp_password("A", 0, 1500).name == "Guest"
        assert session.async_send_dp_raw.await_count == 1
    asyncio.run(run())


@pytest.mark.parametrize("result", [
    None, {"id": 52, "type": 0, "raw": b"\x00\x01"},
    {"id": 52, "type": 0, "raw": b"\x01\x00"},
    {"id": 51, "type": 0, "raw": b"\x00\x00"},
    {"id": 52, "type": 2, "raw": b"\x00\x00"},
    {"id": 52, "type": 0, "raw": b"\x00"},
    {"id": 52, "type": 0, "raw": b"\x00\x00\x00"},
    {"id": 52, "type": 0, "raw": None},
])
def test_missing_rejected_or_wrong_confirmation_remains_pending(monkeypatch, result):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.async_send_dp_raw.return_value = result
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        assert len(store.get_expired_temp_passwords("A", 2000)) == 1
        session.async_send_dp_raw.return_value = {"id": 52, "type": 0, "raw": b"\x00\x00"}
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 1
    asyncio.run(run())


def test_offline_unsupported_future_unconfirmed_and_other_lock_are_untouched(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.is_connected = False
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        session.is_connected = True
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", None) == 0
        assert await cleanup.async_cleanup_expired_passwords(store, session, "B", 52) == 0
        monkeypatch.setattr(credential_store.time, "time", lambda: 1999)
        await store.async_add_temp_password("A", "Legacy", 0, 1500)
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())


def test_reused_slot_never_deletes_new_code(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        new = await store.async_add_temp_password("A", "New guest", 2000, 3000, hw_id=0)
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        session.async_send_dp_raw.assert_not_awaited()
        assert store.resolve_temp_password("A", 0, 2500).password_id == new.password_id
    asyncio.run(run())


def test_exception_or_disk_failure_is_retriable(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.async_send_dp_raw.side_effect = TimeoutError()
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        session.async_send_dp_raw.side_effect = None
        store.async_save = AsyncMock(side_effect=OSError())
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        assert store._data["temp_passwords"][rec.password_id]["removed_at"] is None
        store.async_save = AsyncMock()
        session.async_send_dp_raw.return_value["raw"] = b"\x00\x02"
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 1
    asyncio.run(run())


def test_reset_during_response_does_not_restore_old_records(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        async def send(*_):
            await store.async_report_factory_reset("A")
            return {"id": 52, "type": 0, "raw": b"\x00\x00"}
        session.async_send_dp_raw.side_effect = send
        assert await cleanup.async_cleanup_expired_passwords(store, session, "A", 52) == 0
        assert store._data["temp_passwords"] == {}
    asyncio.run(run())


def test_normal_connection_cleans_without_extra_connect_and_throttles_retry(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch, b"\x00\x01")
        c = coordinator(monkeypatch, store)
        c._profile["services"] = {"delete_temp_password": {"dp": 52}}
        c._temp_password_lock = asyncio.Lock()
        c._temp_cleanup_retry_at = 0
        c._session = session
        session.async_connect = AsyncMock(return_value=True)
        c.async_update_listeners = Mock()
        await c._async_ensure_connected()
        await c._async_ensure_connected()
        assert session.async_send_dp_raw.await_count == 1
        session.async_connect.assert_not_awaited()
        c._temp_cleanup_retry_at = 0
        session.async_send_dp_raw.return_value["raw"] = b"\x00\x00"
        await c._async_ensure_connected()
        c.async_update_listeners.assert_called_once()
        assert session.async_send_dp_raw.await_count == 2
    asyncio.run(run())


def test_startup_connection_also_cleans(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        c = coordinator(monkeypatch, store)
        c._profile["services"] = {"delete_temp_password": {"dp": 52}}
        c._op_lock = asyncio.Lock()
        c._temp_password_lock = asyncio.Lock()
        c._temp_cleanup_retry_at = 0
        c._session = session
        session.async_connect_single_attempt = AsyncMock(return_value=True)
        c._fetch_status = AsyncMock()
        c._reset_idle_timer = Mock()
        c.async_update_listeners = Mock()
        await c.async_one_shot_status()
        session.async_send_dp_raw.assert_awaited_once_with(52, b"\x00")
    asyncio.run(run())


def test_cleanup_waits_for_creation_then_rechecks_reused_id(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        c = coordinator(monkeypatch, store)
        c._profile["services"] = {"delete_temp_password": {"dp": 52}}
        c._temp_password_lock = asyncio.Lock()
        c._temp_cleanup_retry_at = 0
        c._session = session
        c.async_update_listeners = Mock()
        async with c._temp_password_lock:
            pending = asyncio.create_task(c._async_cleanup_temp_passwords())
            await asyncio.sleep(0)
            assert not pending.done()
            await store.async_add_temp_password("A", "New guest", 2000, 3000, hw_id=0)
        await pending
        session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())
