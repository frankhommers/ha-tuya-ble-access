"""Physical access must never be reported paused on an ambiguous DP3 result."""
import asyncio
import copy
import json
from pathlib import Path
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_credential_store import _fresh_store, _load
from test_credential_schedule import policy

pause = _load('credential_pause')
CAPABILITY = {'dp': 3, 'strategy': 'no_weekdays', 'credential_types': [2, 3]}


async def fixture(monkeypatch, kind=3):
    monkeypatch.setattr(pause.time, 'time', lambda: 1790544600)
    store = _fresh_store()
    rec = await store.async_add_credential(7, 'MAC_A', kind, 9, 'Test access', device_policy=policy(kind))
    session = types.SimpleNamespace(async_send_dp_raw=AsyncMock(return_value={
        'id': 3, 'type': 0, 'raw': bytes([kind, 0, 0, 7, 9, 0, 255])}))
    return store, rec, session


@pytest.mark.parametrize("kind", [2, 3])
def test_pause_resume_exact_policy_survives_restart(monkeypatch, kind):
    async def run():
        store, rec, session = await fixture(monkeypatch, kind)
        original = copy.deepcopy(rec.device_policy)
        result = await pause.async_set_credential_paused(store, session, 'MAC_A', rec.credential_id, CAPABILITY, True)
        assert result['pause_state'] == 'paused'
        restarted = _fresh_store(); restarted._data = copy.deepcopy(store._data)
        assert restarted.find_credential('MAC_A', kind, 9).pause_state == 'paused'
        await pause.async_set_credential_paused(restarted, session, 'MAC_A', rec.credential_id, CAPABILITY, False)
        assert session.async_send_dp_raw.await_args.args[1][5:22].hex() == original['validity_hex']
        assert restarted.find_credential('MAC_A', kind, 9).device_policy == original
        assert restarted.find_credential('MAC_A', kind, 9).pause_state == 'active'
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout','cancel','wrong_slot','wrong_dp','rejected','empty','save_before','save_after'])
def test_uncertain_outcomes_and_storage_failures(monkeypatch, failure):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        if failure in ('save_before','save_after'):
            store.async_save = AsyncMock(side_effect=[OSError()] if failure == 'save_before' else [None, OSError()])
        elif failure in ('timeout','cancel'):
            session.async_send_dp_raw.side_effect = TimeoutError() if failure == 'timeout' else asyncio.CancelledError()
        else:
            reply = session.async_send_dp_raw.return_value.copy()
            if failure == 'wrong_slot': reply['raw'] = bytes.fromhex('030000070800ff')
            if failure == 'wrong_dp': reply['id'] = 53
            if failure == 'rejected': reply['raw'] = bytes.fromhex('03000007090000')
            session.async_send_dp_raw.return_value = None if failure == 'empty' else reply
        with pytest.raises((pause.CredentialPauseError, TimeoutError, asyncio.CancelledError, OSError)):
            await pause.async_set_credential_paused(store, session, 'MAC_A', rec.credential_id, CAPABILITY, True)
        fresh = store.find_credential('MAC_A', 3, 9)
        assert fresh.pause_state == ('active' if failure == 'save_before' else 'unknown')
        assert fresh.device_policy == rec.device_policy
        assert session.async_send_dp_raw.await_count == (0 if failure == 'save_before' else 1)
    asyncio.run(run())


@pytest.mark.parametrize('case', ['legacy','wrong_lock','expired','unsupported','admin','identity','replaced','duplicate'])
def test_invalid_records_never_send(monkeypatch, case):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        cap = copy.deepcopy(CAPABILITY); lock = 'MAC_A'
        data = store._data['credentials'][rec.credential_id]
        if case == 'legacy': data.pop('device_policy')
        if case == 'wrong_lock': lock = 'MAC_B'
        if case == 'expired': monkeypatch.setattr(pause.time, 'time', lambda: 2000000000)
        if case == 'unsupported': cap['credential_types'] = [2]
        if case == 'admin': data['device_policy']['admin'] = True
        if case == 'identity': data['device_policy']['hw_id'] = 10
        if case == 'replaced': await store.async_add_credential(7,'MAC_A',3,9,'Replacement',device_policy=policy(3))
        if case == 'duplicate': store._data['credentials']['dup'] = {**copy.deepcopy(data),'credential_id':'dup'}
        with pytest.raises(pause.CredentialPauseError):
            await pause.async_set_credential_paused(store, session, lock, rec.credential_id, cap, False)
        session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())


def test_reset_during_write_does_not_resurrect(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch)
        async def send(*_):
            await store.async_report_factory_reset('MAC_A')
            return {'id':3,'type':0,'raw':bytes.fromhex('030000070900ff')}
        session.async_send_dp_raw.side_effect = send
        with pytest.raises(pause.CredentialPauseError):
            await pause.async_set_credential_paused(store, session, 'MAC_A', rec.credential_id, CAPABILITY, True)
        assert not store.get_credentials_for_lock('MAC_A')
    asyncio.run(run())


def test_only_verified_card_and_fingerprint_profile_enabled():
    root = Path(__file__).resolve().parents[1] / 'custom_components/tuya_ble_access/device_profiles'
    enabled = {}
    for p in root.glob('*.json'):
        data = json.loads(p.read_text())
        if isinstance(data, dict) and 'pause_credential' in data.get('services', {}):
            enabled[p.stem] = data['services']['pause_credential']
    assert enabled == {'ba2qk177': CAPABILITY}


def test_services_are_admin_only_serialize_and_disconnect(monkeypatch):
    from test_activation_entrypoints import FakeServiceHass, _entry, services
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.async_disconnect = AsyncMock()
        coord = types.SimpleNamespace(_profile={'services':{'pause_credential':CAPABILITY}},
            _op_lock=asyncio.Lock(), _async_ensure_connected=AsyncMock(), _session=session,
            async_update_listeners=Mock())
        entry = _entry(); entry.runtime_data = types.SimpleNamespace(coordinators={'MAC_A':coord})
        hass = FakeServiceHass([entry]); hass.data = {'tuya_ble_access':{'credential_store':store}}
        await services.async_register_services(hass)
        reg = hass.services.registration('tuya_ble_access','pause_credential')
        assert reg in hass.admin_service_registrations
        call = types.SimpleNamespace(data={'device_id':'MAC_A','credential_id':rec.credential_id})
        started, finish = asyncio.Event(), asyncio.Event()
        async def send(*_):
            assert coord._op_lock.locked()
            started.set(); await finish.wait()
            return {'id':3,'type':0,'raw':bytes.fromhex('030000070900ff')}
        session.async_send_dp_raw.side_effect = send
        task = asyncio.create_task(reg[2](call)); await started.wait()
        reset_handler = hass.services.registration('tuya_ble_access','report_factory_reset')[2]
        reset = asyncio.create_task(reset_handler(types.SimpleNamespace(data={'device_id':'MAC_A'})))
        await asyncio.sleep(0)
        assert not reset.done() and store.get_credentials_for_lock('MAC_A')
        finish.set()
        assert (await task)['pause_state'] == 'paused'
        await reset
        assert not store.get_credentials_for_lock('MAC_A')
        session.async_disconnect.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('scenario', ['resume', 'connect_failure', 'unsupported'])
def test_service_resume_and_failure_cleanup(monkeypatch, scenario):
    from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services
    async def run():
        store, rec, session = await fixture(monkeypatch)
        session.async_disconnect = AsyncMock()
        cap = CAPABILITY if scenario != 'unsupported' else {'dp':3}
        coord = types.SimpleNamespace(_profile={'services':{'pause_credential':cap}},
            _op_lock=asyncio.Lock(), _async_ensure_connected=AsyncMock(), _session=session,
            async_update_listeners=Mock())
        entry = _entry(); entry.runtime_data = types.SimpleNamespace(coordinators={'MAC_A':coord})
        hass = FakeServiceHass([entry]); hass.data = {'tuya_ble_access':{'credential_store':store}}
        await services.async_register_services(hass)
        reg = hass.services.registration('tuya_ble_access','resume_credential')
        assert reg in hass.admin_service_registrations
        call = types.SimpleNamespace(data={'device_id':'MAC_A','credential_id':rec.credential_id})
        if scenario == 'connect_failure': coord._async_ensure_connected.side_effect = TimeoutError()
        if scenario == 'resume':
            assert (await reg[2](call))['pause_state'] == 'active'
            assert session.async_send_dp_raw.await_args.args[1][5:22].hex() == rec.device_policy['validity_hex']
        else:
            with pytest.raises(TimeoutError if scenario == 'connect_failure' else ServiceHomeAssistantError):
                await reg[2](call)
            session.async_send_dp_raw.assert_not_awaited()
        assert session.async_disconnect.await_count == (0 if scenario == 'unsupported' else 1)
        assert not coord._op_lock.locked()
    asyncio.run(run())


def test_pausing_card_preserves_fingerprint_even_with_same_slot_number(monkeypatch):
    async def run():
        store, card, session = await fixture(monkeypatch, kind=2)
        finger = await store.async_add_credential(7, 'MAC_A', 3, 9, 'Other finger', device_policy=policy(3))
        original = copy.deepcopy(store._data['credentials'][finger.credential_id])
        await pause.async_set_credential_paused(store, session, 'MAC_A', card.credential_id, CAPABILITY, True)
        assert session.async_send_dp_raw.await_args.args[1][:5] == bytes([2, 0, 0, 7, 9])
        assert store.find_credential('MAC_A', 2, 9).pause_state == 'paused'
        assert store._data['credentials'][finger.credential_id] == original
    asyncio.run(run())


def test_ordinary_pin_remains_unsupported_after_card_enablement(monkeypatch):
    async def run():
        store, rec, session = await fixture(monkeypatch, kind=1)
        with pytest.raises(pause.CredentialPauseError, match='credential_pause_unsupported'):
            await pause.async_set_credential_paused(store, session, 'MAC_A', rec.credential_id, CAPABILITY, True)
        session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())
