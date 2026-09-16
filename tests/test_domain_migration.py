"""Domain migrations preserve identity and never overwrite unrelated stores."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'tuya_ble_access'


def environment(monkeypatch):
    package = ModuleType('_domain_migration_test')
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    data = {
        'tuya_ble_lock_devices': {'devices': {'MAC': {'local_key': 'synthetic-key'}}},
        'tuya_ble_lock_credentials': {'members': {'1': {'name': 'Guest'}},
                                     'credentials': {'1': {'hw_id': 2}},
                                     'temp_passwords': {'archived': {'removed_at': 123}}},
    }
    source = NS(entry_id='old', domain='tuya_ble_lock', version=2,
                pref_disable_new_entities=False, pref_disable_polling=True, setup_lock=asyncio.Lock())
    entry = NS(entry_id='new', data={'setup_method': 'local',
               'legacy_domain_migration': {'entry_id': 'old'}})
    devices = [NS(id='hub', config_entry_id='old', identifiers={('tuya_ble_lock', 'old')}),
               NS(id='lock', config_entry_id='old', identifiers={('tuya_ble_lock', 'MAC')},
                  via_device_id='hub', name_by_user='My keybox')]
    entities = [NS(entity_id='lock.my_keybox', unique_id='MAC_lock', platform='tuya_ble_lock',
                   config_entry_id='old', device_id='lock', disabled_by='config_entry'),
                NS(entity_id='sensor.custom', unique_id='MAC_sensor', platform='tuya_ble_lock',
                   config_entry_id='old', device_id='lock', disabled_by='user')]
    state = NS(data=data, source=source, entry=entry, devices=devices, entities=entities,
               fail_save=None, unload=True, fail_entity=False, removed_services=[])

    class Store:
        def __init__(self, hass, version, key): self.key = key
        async def async_load(self): return deepcopy(data.get(self.key))
        async def async_save(self, value):
            if state.fail_save == self.key: raise OSError('simulated disk failure')
            data[self.key] = deepcopy(value)

    class Entries:
        def async_get_entry(self, entry_id): return state.source
        async def async_set_disabled_by(self, entry_id, reason): return state.unload
        async def async_unload(self, entry_id): return state.unload
        def async_update_entry(self, entry, **kwargs):
            for key, value in kwargs.items(): setattr(entry, key, value)
        async def async_remove(self, entry_id):
            assert all(d.config_entry_id != entry_id for d in devices)
            assert all(e.config_entry_id != entry_id for e in entities)
            state.source = None
            return {'require_restart': False}

    class Devices:
        def async_update_device(self, device_id, *, new_config_entry_id, new_identifiers):
            d = next(d for d in devices if d.id == device_id)
            # HA removes still-associated entities when device ownership changes.
            entities[:] = [e for e in entities if not (e.device_id == device_id and e.config_entry_id == d.config_entry_id)]
            d.config_entry_id = new_config_entry_id
            d.identifiers = new_identifiers

    class Entities:
        def async_get(self, entity_id):
            return next((e for e in entities if e.entity_id == entity_id), None)
        def async_update_entity_platform(self, entity_id, platform, *, new_config_entry_id, new_device_id):
            if state.fail_entity: raise RuntimeError('simulated registry failure')
            e = next(e for e in entities if e.entity_id == entity_id)
            e.platform = platform
            e.device_id = new_device_id
            e.config_entry_id = new_config_entry_id
        def async_update_entity(self, entity_id, **kwargs):
            e = next(e for e in entities if e.entity_id == entity_id)
            for key, value in kwargs.items(): setattr(e, key, value)

    dr = NS(async_get=lambda h: Devices(), async_entries_for_config_entry=lambda r, i: [d for d in devices if d.config_entry_id == i])
    er = NS(async_get=lambda h: Entities(), async_entries_for_config_entry=lambda r, i: [e for e in entities if e.config_entry_id == i], RegistryEntryDisabler=NS(CONFIG_ENTRY='config_entry'))
    class MigrationError(ValueError):
        def __init__(self, *, translation_domain, translation_key):
            super().__init__(translation_key)

    replacements = {
        'homeassistant.config_entries': NS(ConfigEntryDisabler=NS(USER='user')),
        'homeassistant.exceptions': NS(ConfigEntryError=MigrationError),
        'homeassistant.helpers': NS(device_registry=dr, entity_registry=er),
        'homeassistant.helpers.storage': NS(Store=Store),
    }
    for key, value in replacements.items(): monkeypatch.setitem(sys.modules, key, value)
    spec = importlib.util.spec_from_file_location(package.__name__ + '.migration', ROOT / 'migration.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state.hass = NS(config_entries=Entries(), services=NS(
        async_services=lambda: {'tuya_ble_lock': {'unlock': {}}},
        async_remove=lambda domain, service: state.removed_services.append((domain, service))))
    state.run = lambda: asyncio.run(module.async_migrate_domain(state.hass, entry))
    return state


def test_migration_preserves_ids_credentials_archive_and_user_disabling(monkeypatch):
    s = environment(monkeypatch)
    old = deepcopy(s.data)
    s.run()
    assert s.source is None
    assert s.entry.data == {'setup_method': 'local'}
    for key, value in old.items():
        assert s.data[key] == value
        assert s.data[key.replace('tuya_ble_lock', 'tuya_ble_access')] == value
    assert s.devices[0].identifiers == {('tuya_ble_access', 'new')}
    assert s.devices[1].id == 'lock' and s.devices[1].via_device_id == 'hub'
    assert s.entities[0].entity_id == 'lock.my_keybox'
    assert s.entities[0].device_id == 'lock' and s.entities[0].unique_id == 'MAC_lock'
    assert s.entities[0].disabled_by is None
    assert s.entities[1].disabled_by == 'user'
    assert s.entry.pref_disable_polling is True
    assert s.removed_services == [('tuya_ble_lock', 'unlock')]
    s.run()  # Completed migration is a no-op.


@pytest.mark.parametrize('failure', ['save', 'registry'])
def test_interrupted_migration_can_resume(monkeypatch, failure):
    s = environment(monkeypatch)
    if failure == 'save': s.fail_save = 'tuya_ble_access_credentials'
    else: s.fail_entity = True
    with pytest.raises((OSError, RuntimeError)): s.run()
    assert s.source is not None
    assert 'legacy_domain_migration' in s.entry.data
    s.fail_save = None
    s.fail_entity = False
    s.run()
    assert s.source is None and 'legacy_domain_migration' not in s.entry.data
    assert all(e.config_entry_id == 'new' for e in s.entities)


def test_conflicting_new_storage_is_not_overwritten(monkeypatch):
    s = environment(monkeypatch)
    s.data['tuya_ble_access_devices'] = {'devices': {'OTHER': {}}}
    with pytest.raises(ValueError, match='storage_conflict'): s.run()
    assert s.data['tuya_ble_access_devices'] == {'devices': {'OTHER': {}}}
    assert s.source is not None
    assert all(d.config_entry_id == 'old' for d in s.devices)


def test_unload_failure_does_not_move_or_copy_data(monkeypatch):
    s = environment(monkeypatch)
    s.unload = False
    with pytest.raises(ValueError, match='unload_failed'): s.run()
    assert not any(k.startswith('tuya_ble_access') for k in s.data)
    assert all(e.config_entry_id == 'old' for e in s.entities)


def test_missing_source_requires_completed_checkpoint(monkeypatch):
    s = environment(monkeypatch)
    s.source = None
    with pytest.raises(ValueError, match='missing'): s.run()
    s.entry.data['legacy_domain_migration']['registries_migrated'] = True
    s.run()
    assert 'legacy_domain_migration' not in s.entry.data
