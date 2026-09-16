"""Move the previous integration to the new domain using HA registry APIs."""

from copy import deepcopy

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY, STORAGE_KEY_DEVICES, STORAGE_VERSION

LEGACY_DOMAIN = "tuya_ble_lock"
MIGRATION_KEY = "legacy_domain_migration"


async def async_migrate_domain(hass, entry):
    """Resume a confirmed migration before any new entities or BLE sessions load."""
    marker = entry.data.get(MIGRATION_KEY)
    if not marker:
        return
    old_id = marker["entry_id"]
    source = hass.config_entries.async_get_entry(old_id)
    if source is None:
        if not marker.get("registries_migrated"):
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_missing_source")
    else:
        if source.domain != LEGACY_DOMAIN or source.version != 2:
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_unsupported_source")
        # Stop the old integration and prevent it reopening BLE connections on restart.
        await hass.config_entries.async_set_disabled_by(
            old_id, ConfigEntryDisabler.USER
        )
        # During startup, disabling can return before the old component finishes
        # loading. Wait for it, then explicitly verify unloading (also on retry).
        async with source.setup_lock:
            pass
        if not await hass.config_entries.async_unload(old_id):
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_unload_failed")

        for old_key, new_key in (
            ("tuya_ble_lock_devices", STORAGE_KEY_DEVICES),
            ("tuya_ble_lock_credentials", STORAGE_KEY),
        ):
            old_data = await Store(hass, STORAGE_VERSION, old_key).async_load()
            target = Store(hass, STORAGE_VERSION, new_key)
            new_data = await target.async_load()
            if new_data is not None and new_data != old_data:
                raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_storage_conflict")
            if old_data is not None and new_data is None:
                await target.async_save(deepcopy(old_data))

        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        # HA removes entities when their device moves to a different config entry.
        # Detach and migrate entities first, move devices, then restore device links.
        # Persist links so an interrupted move can resume without losing identity.
        links = marker.get("entity_devices")
        if links is None:
            links = {e.entity_id: e.device_id for e in
                     er.async_entries_for_config_entry(entities, old_id)}
            marker = {**marker, "entity_devices": links}
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, MIGRATION_KEY: marker},
            )
        for entity_id in links:
            entity = entities.async_get(entity_id)
            if entity is None:
                raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_entity_missing")
            if entity.platform == LEGACY_DOMAIN:
                entities.async_update_entity_platform(
                    entity_id, DOMAIN, new_config_entry_id=entry.entry_id,
                    new_device_id=None,
                )
                if entity.disabled_by == er.RegistryEntryDisabler.CONFIG_ENTRY:
                    entities.async_update_entity(entity_id, disabled_by=None)
            elif entity.platform != DOMAIN or entity.config_entry_id != entry.entry_id:
                raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_entity_conflict")
        for device in list(dr.async_entries_for_config_entry(devices, old_id)):
            identifiers = {
                (DOMAIN, entry.entry_id if value == old_id else value)
                if domain == LEGACY_DOMAIN else (domain, value)
                for domain, value in device.identifiers
            }
            devices.async_update_device(
                device.id, new_config_entry_id=entry.entry_id,
                new_identifiers=identifiers,
            )
        for entity_id, device_id in links.items():
            entities.async_update_entity(entity_id, device_id=device_id)

        # Checkpoint before removal: retry can finish if removal succeeded but saving
        # the final entry update was interrupted. Old Store files remain untouched.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, MIGRATION_KEY: {
                "entry_id": old_id, "registries_migrated": True,
            }},
            pref_disable_new_entities=source.pref_disable_new_entities,
            pref_disable_polling=source.pref_disable_polling,
        )
        result = await hass.config_entries.async_remove(old_id)
        if not result.get("require_restart", False):
            # Old services close over old runtime data. Never leave them callable.
            for service in list(hass.services.async_services().get(LEGACY_DOMAIN, {})):
                hass.services.async_remove(LEGACY_DOMAIN, service)
        else:
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="migration_restart_required")

    data = dict(entry.data)
    data.pop(MIGRATION_KEY)
    hass.config_entries.async_update_entry(entry, data=data)
