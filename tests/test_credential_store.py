"""Tests for credential-store dedup and clear.

Re-enrolling a hardware slot (lock, cred_type, hw_id) must replace the old
entry instead of stacking a duplicate — otherwise the store fills with stale
duplicates after re-pairs and unlock events resolve to the wrong member.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _stub_ha():
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = object
    sys.modules["homeassistant.helpers.storage"] = storage
    ha.helpers = helpers
    helpers.storage = storage


def _load(name: str):
    pkg = sys.modules.setdefault("_tblcs", types.ModuleType("_tblcs"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblcs.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(fq, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fq] = module
    spec.loader.exec_module(module)
    return module


_stub_ha()
_load("const")
_load("models")
credential_store = _load("credential_store")


def _fresh_store():
    store = credential_store.CredentialStore.__new__(credential_store.CredentialStore)
    store._data = {"version": 1, "members": {}, "credentials": {}, "temp_passwords": {}}
    store._store = types.SimpleNamespace()

    async def _save():
        return None

    store.async_save = _save  # bypass HA Store
    return store


FP = 3  # CRED_FINGERPRINT


def test_reenrolling_same_slot_replaces_not_duplicates():
    async def run():
        store = _fresh_store()
        await store.async_add_credential(1, "MAC_A", FP, 1, "Left index")
        await store.async_add_credential(1, "MAC_A", FP, 1, "Right middle")  # same slot
        creds = store._data["credentials"]
        assert len(creds) == 1
        only = next(iter(creds.values()))
        assert only["hw_id"] == 1 and only["name"] == "Right middle"

    asyncio.run(run())


def test_distinct_slots_and_locks_coexist():
    async def run():
        store = _fresh_store()
        await store.async_add_credential(1, "MAC_A", FP, 1, "a")
        await store.async_add_credential(1, "MAC_A", FP, 2, "b")   # other slot
        await store.async_add_credential(1, "MAC_B", FP, 1, "c")   # other lock
        assert len(store._data["credentials"]) == 3

    asyncio.run(run())


def test_clear_one_lock_leaves_others():
    async def run():
        store = _fresh_store()
        await store.async_add_credential(1, "MAC_A", FP, 1, "a")
        await store.async_add_credential(1, "MAC_B", FP, 1, "c")
        removed = await store.async_clear(lock_entry_id="MAC_A")
        assert removed == 1
        macs = {c["lock_entry_id"] for c in store._data["credentials"].values()}
        assert macs == {"MAC_B"}

    asyncio.run(run())


def test_clear_all_wipes_credentials_and_members():
    async def run():
        store = _fresh_store()
        store._data["members"]["1"] = {"member_id": 1, "name": "x",
                                       "ha_user_id": None, "created_at": 0.0}
        await store.async_add_credential(1, "MAC_A", FP, 1, "a")
        removed = await store.async_clear()
        assert removed == 1
        assert store._data["credentials"] == {}
        assert store._data["members"] == {}

    asyncio.run(run())


# ---- Unlock attribution ----------------------------------------------------

def test_unlock_id_resolves_to_the_specific_slot():
    """Verified on the real lock: the unlock DP carries the credential slot, so
    two fingers of one person are distinguishable and must be named exactly."""
    async def run():
        store = _fresh_store()
        store._data["members"]["1"] = {
            "member_id": 1, "name": "Tuya Lock Test",
            "ha_user_id": None, "created_at": 0.0,
            "person_entity_id": "person.tuya_lock_test",
        }
        await store.async_add_credential(1, "MAC_A", FP, 1, "Left thumb")
        await store.async_add_credential(1, "MAC_A", FP, 2, "Right index")

        # Slot wins even though a member with id 1 also exists.
        member, cred_name = store.resolve_unlock("MAC_A", FP, 1)
        assert member.name == "Tuya Lock Test"
        assert cred_name == "Left thumb"

        member, cred_name = store.resolve_unlock("MAC_A", FP, 2)
        assert cred_name == "Right index"

    asyncio.run(run())


def test_unknown_slot_falls_back_to_member_with_generic_label():
    """A slot HA never registered still resolves to the person if the value
    matches a member; the finger itself is then genuinely unknown."""
    async def run():
        store = _fresh_store()
        store._data["members"]["4"] = {
            "member_id": 4, "name": "Frank",
            "ha_user_id": None, "created_at": 0.0,
        }
        await store.async_add_credential(4, "MAC_A", FP, 9, "Right thumb")

        member, cred_name = store.resolve_unlock("MAC_A", FP, 4)
        assert member.name == "Frank"
        assert cred_name == "Fingerprint"

    asyncio.run(run())


def test_completely_unknown_value_resolves_to_nothing():
    async def run():
        store = _fresh_store()
        assert store.resolve_unlock("MAC_A", FP, 42) == (None, None)

    asyncio.run(run())


def test_factory_reset_clears_only_selected_lock_and_persists():
    async def run():
        store = _fresh_store()
        member = await store.async_add_member("Frank", person_entity_id="person.frank")
        for mac in ("MAC_A", "MAC_B"):
            for cred_type in (1, 2, 3, 4):
                await store.async_add_credential(member.member_id, mac, cred_type, 1, "Access")
            await store.async_add_temp_password(mac, "Guest", 100, 200)
        before = copy.deepcopy(store._data)
        saved = []

        async def save():
            saved.append(copy.deepcopy(store._data))

        store.async_save = save
        assert await store.async_report_factory_reset("MAC_A") == {
            "credentials": 4, "temp_passwords": 1,
        }
        assert store._data["members"] == before["members"]
        assert store.get_credentials_for_lock("MAC_A") == []
        for key in ("credentials", "temp_passwords"):
            assert store._data[key] == {
                k: v for k, v in before[key].items() if v["lock_entry_id"] == "MAC_B"
            }
        assert saved[-1] == store._data
        # Retained members must not identify a newly reused slot on this lock.
        assert store.resolve_unlock("MAC_A", FP, member.member_id) == (None, None)
        assert store.resolve_unlock("MAC_B", FP, 1)[0].name == "Frank"

        reloaded = _fresh_store()
        reloaded._data = saved[-1]
        assert reloaded.resolve_unlock("MAC_A", FP, 1) == (None, None)
        assert await reloaded.async_report_factory_reset("MAC_A") == {
            "credentials": 0, "temp_passwords": 0,
        }
        await reloaded.async_add_credential(member.member_id, "MAC_A", FP, 2, "New finger")
        assert reloaded.resolve_unlock("MAC_A", FP, 2)[1] == "New finger"

    asyncio.run(run())


@pytest.mark.parametrize("failure", [OSError("disk unavailable"), asyncio.CancelledError()])
def test_factory_reset_save_failure_keeps_local_records(failure):
    async def run():
        store = _fresh_store()
        await store.async_add_credential(1, "MAC_A", FP, 1, "Finger")
        await store.async_add_temp_password("MAC_A", "Guest", 100, 200)
        before = copy.deepcopy(store._data)

        async def fail_save():
            raise failure

        store.async_save = fail_save
        with pytest.raises(type(failure)):
            await store.async_report_factory_reset("MAC_A")
        assert store._data == before

    asyncio.run(run())


@pytest.mark.parametrize("lock_id", [None, ""])
def test_factory_reset_requires_a_lock(lock_id):
    async def run():
        store = _fresh_store()
        await store.async_add_credential(1, "MAC_A", FP, 1, "Finger")
        with pytest.raises(ValueError, match="lock is required"):
            await store.async_report_factory_reset(lock_id)
        assert len(store.get_credentials_for_lock("MAC_A")) == 1

    asyncio.run(run())
