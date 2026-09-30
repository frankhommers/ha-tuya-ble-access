"""Manager rows expose presentation data, and edits preserve lock credentials."""
import asyncio
import copy
import json
import types
from unittest.mock import AsyncMock, Mock

import pytest
from test_credential_store import _fresh_store, _load
from test_credential_schedule import policy

view = _load('access_view')
PROFILE = {'services': {'pause_credential': {'dp':3,'strategy':'no_weekdays','credential_types':[1,2,3]},
                        'pause_temp_password': {'dp':53,'strategy':'no_weekdays'}}}


def test_manager_before_any_lock_has_loaded():
    from test_activation_entrypoints import FakeServiceHass, services
    async def run():
        hass = FakeServiceHass([])
        await services.async_register_services(hass)
        handler = hass.services.registration('tuya_ble_access', 'list_access')[2]
        assert await handler(types.SimpleNamespace(data={})) == {
            'locks': [], 'items': [], 'device_id': None,
        }
    asyncio.run(run())


def test_metadata_edit_preserves_secret_policy_id_and_other_credentials():
    async def run():
        store = _fresh_store()
        member = await store.async_add_member('Original', person_entity_id='person.original')
        rec = await store.async_add_credential(member.member_id,'LOCK',1,9,'Old label',device_policy=policy(1),pin_code='001234')
        other = await store.async_add_credential(member.member_id,'LOCK',2,8,'Card',device_policy=policy(2))
        before = copy.deepcopy(store._data['credentials'][rec.credential_id])
        await store.async_update_access('LOCK',rec.credential_id,{'name':'New label','person':'person.new'})
        now = store.find_credential('LOCK',1,9)
        assert now.credential_id == rec.credential_id and now.pin_code == rec.pin_code
        assert now.device_policy == rec.device_policy and now.member_id == rec.member_id
        assert store.credential_person(now) == 'person.new'
        assert store.credential_person(other) == 'person.original'
        await store.async_update_access('LOCK',rec.credential_id,{'person':None})
        assert store.credential_person(store.find_credential('LOCK',1,9)) is None
        assert store.get_member(member.member_id).person_entity_id == 'person.original'
        with pytest.raises(KeyError):
            await store.async_update_access('OTHER',rec.credential_id,{'name':'Wrong lock'})
        assert store.find_credential('LOCK',1,9).name == 'New label'
        store.async_save = AsyncMock(side_effect=OSError())
        with pytest.raises(OSError):
            await store.async_update_access('LOCK',rec.credential_id,{'name':'Failed'})
        assert store.find_credential('LOCK',1,9).name == 'New label'
        for key in ('device_policy','pin_code','member_id','hw_id','created_at'):
            assert store._data['credentials'][rec.credential_id][key] == before[key]
    asyncio.run(run())


def test_public_list_no_pin_protocol_fields_and_all_temporary_states(monkeypatch):
    async def run():
        monkeypatch.setattr(view.time,'time',lambda:1790544600)
        store = _fresh_store()
        await store.async_add_credential(7,'LOCK',1,9,'PIN',device_policy=policy(1),pin_code='001234')
        await store.async_add_credential(7,'OTHER',1,9,'Other',device_policy=policy(1),pin_code='001235')
        legacy = await store.async_add_credential(7,'LOCK',3,8,'Legacy')
        paused = await store.async_add_credential(7,'LOCK',2,7,'Paused',device_policy={**policy(2),'hw_id':7})
        await store.async_set_credential_pause_state(paused,'paused',True)
        expired = await store.async_add_temp_password('LOCK','Expired',1790540000,1790541000,1)
        active = await store.async_add_temp_password('LOCK','Temporary',1790540000,1790550000,2)
        removed = await store.async_add_temp_password('LOCK','Removed',1790540000,1790550000,3)
        await store.async_archive_temp_password(removed.password_id,1790542000)
        await store.async_update_access('LOCK',active.password_id,{'name':'Visitor','person':'person.visitor'})
        rows = view.access_items(store,'LOCK',PROFILE)
        assert len(rows) == 6
        data = json.dumps(rows)
        for hidden in ('001234','001235','pin_code','hw_id','device_policy','member_id','validity_hex'):
            assert hidden not in data
        assert next(r for r in rows if r['name']=='PIN')['needs_pin'] is False
        assert next(r for r in rows if r['id']==legacy.credential_id)['pause_reason']=='credential_policy_unknown'
        assert next(r for r in rows if r['id']==paused.credential_id)['can_resume'] is True
        assert next(r for r in rows if r['id']==expired.password_id)['status']=='expired'
        assert next(r for r in rows if r['id']==removed.password_id)['status']=='removed'
        assert next(r for r in rows if r['id']==active.password_id)['person']=='person.visitor'
    asyncio.run(run())


def test_manager_services_admin_only_and_edit_does_not_connect(monkeypatch):
    from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services
    async def run():
        store = _fresh_store()
        rec = await store.async_add_credential(7,'MAC_A',1,9,'PIN',device_policy=policy(1),pin_code='001234')
        coord = types.SimpleNamespace(device_name='Keybox',_profile=PROFILE,async_update_listeners=Mock(),_async_ensure_connected=AsyncMock())
        entry = _entry(); entry.runtime_data=types.SimpleNamespace(coordinators={'MAC_A':coord})
        hass=FakeServiceHass([entry]);hass.data={'tuya_ble_access':{'credential_store':store}}
        hass.states=types.SimpleNamespace(get=lambda eid: object() if eid=='person.test' else None)
        await services.async_register_services(hass)
        for service in ('list_access','update_access'):
            assert hass.services.registration('tuya_ble_access',service) in hass.admin_service_registrations
        update=hass.services.registration('tuya_ble_access','update_access')[2]
        await update(types.SimpleNamespace(data={'device_id':'MAC_A','access_id':rec.credential_id,'name':'Visitor','person':'person.test'}))
        listing=await hass.services.registration('tuya_ble_access','list_access')[2](types.SimpleNamespace(data={}))
        assert listing['items'][0]['person']=='person.test'
        assert '001234' not in json.dumps(listing)
        for changes in ({'name':''},{'person':'sensor.nope'}):
            with pytest.raises(ServiceHomeAssistantError):
                await update(types.SimpleNamespace(data={'device_id':'MAC_A','access_id':rec.credential_id,**changes}))
        coord._async_ensure_connected.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('outcome',['success','rejected','wrong_lock','unknown_policy'])
def test_delete_keeps_record_until_exact_device_confirmation(monkeypatch,outcome):
    from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services
    commands=_load('ble_commands')
    monkeypatch.setattr(services,'build_delete_payload',commands.build_delete_payload)
    async def run():
        store=_fresh_store()
        rec=await store.async_add_credential(2,'MAC_A',1,9,'PIN',device_policy=policy(1),pin_code='001234')
        if outcome=='unknown_policy':store._data['credentials'][rec.credential_id]['device_policy']=None
        expected=bytes.fromhex('010000070901')  # Device member, not HA attribution member 2.
        reply={'id':2,'type':0,'raw':expected+(b'\xff' if outcome=='success' else b'\x00')}
        session=types.SimpleNamespace(async_send_dp_raw=AsyncMock(return_value=reply),async_disconnect=AsyncMock())
        coord=types.SimpleNamespace(profile={'services':{'delete_credential':{'dp':2}}},_async_ensure_connected=AsyncMock(),_session=session,async_update_listeners=Mock())
        entry=_entry();entry.runtime_data=types.SimpleNamespace(coordinators={'MAC_A':coord,'MAC_B':coord})
        hass=FakeServiceHass([entry]);hass.data={'tuya_ble_access':{'credential_store':store}}
        await services.async_register_services(hass)
        reg=hass.services.registration('tuya_ble_access','delete_credential')
        assert reg in hass.admin_service_registrations
        call=types.SimpleNamespace(data={'device_id':'MAC_B' if outcome=='wrong_lock' else 'MAC_A','credential_id':rec.credential_id})
        if outcome=='success':
            await reg[2](call)
            assert not store.get_credentials_for_lock('MAC_A')
            assert session.async_send_dp_raw.await_args.args==(2,expected)
        else:
            with pytest.raises(ServiceHomeAssistantError):await reg[2](call)
            assert store.find_credential('MAC_A',1,9).pin_code=='001234'
            if outcome in ('wrong_lock','unknown_policy'):session.async_send_dp_raw.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('answered',[True,False])
def test_full_sync_includes_pin_and_never_treats_silence_as_empty(monkeypatch,answered):
    from test_activation_entrypoints import FakeServiceHass, ServiceHomeAssistantError, _entry, services
    commands=_load('ble_commands')
    monkeypatch.setattr(services,'parse_credential_list',commands.parse_credential_list)
    monkeypatch.setattr(services,'SYNC_MARKER',commands.SYNC_MARKER)
    async def run():
        store=_fresh_store()
        # Synthetic IDs; includes a PIN omitted by a one-byte query on real firmware.
        replies=[{'id':54,'type':0,'raw':bytes.fromhex('0000070302010802030109010401')},
                 {'id':54,'type':0,'raw':bytes.fromhex('0101')}] if answered else []
        session=types.SimpleNamespace(async_send_dp_raw_long=AsyncMock(return_value=replies),async_disconnect=AsyncMock())
        coord=types.SimpleNamespace(profile={'services':{'sync_credentials':{'dp':54}}},_async_ensure_connected=AsyncMock(),_session=session,async_update_listeners=Mock(),last_credential_sync={'previous':[]})
        entry=_entry();entry.runtime_data=types.SimpleNamespace(coordinators={'MAC_A':coord})
        hass=FakeServiceHass([entry]);hass.data={'tuya_ble_access':{'credential_store':store}}
        await services.async_register_services(hass)
        handler=hass.services.registration('tuya_ble_access','sync_credentials')[2]
        call=types.SimpleNamespace(data={'device_id':'MAC_A'})
        if answered:
            result=await handler(call)
            assert result['credentials']['pin'][0]['hw_id']==9
            assert result['credentials']['pin'][0]['lock_member_id']==4
        else:
            with pytest.raises(ServiceHomeAssistantError):await handler(call)
            assert coord.last_credential_sync=={'previous':[]}
        session.async_send_dp_raw_long.assert_awaited_once_with(54,bytes.fromhex('030102'),timeout=8.0)
        session.async_disconnect.assert_awaited_once()
    asyncio.run(run())
