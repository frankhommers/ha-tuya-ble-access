"""Tuya BLE Smart Lock integration — hub-based architecture.

One cloud or local config entry. All locks are devices under that hub.
Per-device BLE credentials are stored in a separate DeviceStore.
"""

from __future__ import annotations

from types import SimpleNamespace

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_COUNTRY,
    CONF_TUYA_REGION,
)
from .credential_store import CredentialStore
from .device_store import DeviceStore
from .device_profiles import async_load_profile
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)

# All platforms — switch is always loaded (persistent connection switch)
ALL_PLATFORMS = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
]

_BINARY_SENSOR_KEYS = {"doorbell", "hijack", "message"}


def _platforms_for_devices(profiles: dict[str, dict]) -> list[Platform]:
    """Determine which platforms to load based on all device profiles."""
    platforms = {Platform.LOCK, Platform.SENSOR, Platform.BUTTON, Platform.SWITCH}
    for profile in profiles.values():
        entities = profile.get("entities", {})
        select_keys = ("volume_select", "language_select", "unlock_mode_select")
        if any(k in entities for k in select_keys):
            platforms.add(Platform.SELECT)
        if "auto_lock_time_number" in entities:
            platforms.add(Platform.NUMBER)
        state_keys = {m.get("key") for m in profile.get("state_map", {}).values()}
        if state_keys & _BINARY_SENSOR_KEYS:
            platforms.add(Platform.BINARY_SENSOR)
    return list(platforms)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    from .services import async_register_services

    # Migrate legacy per-device entries (v1) to hub model (v2)
    await _async_migrate_legacy_entries(hass)

    await async_register_services(hass)
    return True


async def _async_migrate_legacy_entries(hass: HomeAssistant) -> None:
    """Merge old per-device config entries into a single hub entry."""
    entries = hass.config_entries.async_entries(DOMAIN)
    legacy = [e for e in entries if e.version < 2]
    if not legacy:
        return

    _LOGGER.info("Migrating %d legacy config entries to hub model", len(legacy))

    # Load or create device store
    device_store = DeviceStore(hass)
    await device_store.async_load()

    # Collect cloud creds from first entry that has them
    cloud_creds = {}
    for entry in legacy:
        opts = entry.options or {}
        if opts.get(CONF_TUYA_EMAIL):
            cloud_creds = {
                CONF_TUYA_EMAIL: opts[CONF_TUYA_EMAIL],
                CONF_TUYA_PASSWORD: opts.get(CONF_TUYA_PASSWORD, ""),
                CONF_TUYA_COUNTRY: opts.get(CONF_TUYA_COUNTRY, ""),
                CONF_TUYA_REGION: opts.get(CONF_TUYA_REGION, ""),
            }
            break

    # Move all device data to the store and update credential records
    from .credential_store import CredentialStore
    cred_store = CredentialStore(hass)
    await cred_store.async_load()

    for entry in legacy:
        mac = entry.data.get("device_mac", "").upper()
        if mac and not device_store.get_device(mac):
            await device_store.async_add_device(mac, {
                "uuid": entry.data.get("device_uuid", ""),
                "login_key": entry.data.get("login_key", ""),
                "virtual_id": entry.data.get("virtual_id", ""),
                "auth_key": entry.data.get("auth_key", ""),
                "product_id": entry.data.get("product_id", ""),
                "name": entry.title,
            })
        # Update credential records: old entry_id -> MAC
        for cid, cdata in list(cred_store._data.get("credentials", {}).items()):
            if cdata.get("lock_entry_id") == entry.entry_id:
                cdata["lock_entry_id"] = mac
        for pid, pdata in list(cred_store._data.get("temp_passwords", {}).items()):
            if pdata.get("lock_entry_id") == entry.entry_id:
                pdata["lock_entry_id"] = mac
    await cred_store.async_save()

    # Convert first entry to hub format (v2)
    hub_entry = legacy[0]
    hass.config_entries.async_update_entry(
        hub_entry,
        data=cloud_creds,
        options={},
        version=2,
        unique_id=cloud_creds.get(CONF_TUYA_EMAIL, "tuya_ble_access_hub"),
        title="Tuya BLE Access",
    )

    # Remove remaining legacy entries
    for entry in legacy[1:]:
        await hass.config_entries.async_remove(entry.entry_id)

    _LOGGER.info(
        "Migration complete: hub entry=%s, %d devices in store",
        hub_entry.entry_id, len(device_store.devices),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .migration import async_migrate_domain

    await async_migrate_domain(hass, entry)
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Load stores
    device_store = DeviceStore(hass)
    await device_store.async_load()

    if "credential_store" not in hass.data[DOMAIN]:
        cred_store = CredentialStore(hass)
        await cred_store.async_load()
        hass.data[DOMAIN]["credential_store"] = cred_store
    credential_store = hass.data[DOMAIN]["credential_store"]

    # Register hub device in device registry
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Tuya BLE Access",
        manufacturer="Tuya",
        model="BLE Lock Hub",
    )

    # Create coordinators for each known device
    from .ble_session import TuyaBLELockSession
    from .coordinator import TuyaBLELockCoordinator

    coordinators: dict[str, TuyaBLELockCoordinator] = {}
    adv_unsubscribes: list = []
    profiles: dict[str, dict] = {}

    for mac, dev_data in device_store.devices.items():
        ble_device = bluetooth.async_ble_device_from_address(hass, mac, connectable=True)
        if not ble_device:
            # The lock may simply not be advertising this second. Do NOT skip:
            # that would leave the device with no entities at all until a lucky
            # restart. Stand in with the address (the session only ever needs
            # that and re-resolves the real device on every connect), so the
            # entities exist and go available as soon as we connect.
            _LOGGER.debug(
                "BLE device %s not visible at startup; will resolve on connect",
                mac,
            )
            ble_device = SimpleNamespace(
                address=mac, name=dev_data.get("name", mac), details={}
            )

        login_key = bytes.fromhex(dev_data.get("login_key", ""))
        virtual_id = bytes.fromhex(dev_data.get("virtual_id", ""))
        auth_key_str = dev_data.get("auth_key") or ""
        auth_random_str = dev_data.get("auth_random") or ""
        device_uuid = dev_data.get("uuid", "")
        product_id = dev_data.get("product_id")
        device_name = dev_data.get("name", mac)
        local_key_str = dev_data.get("local_key") or ""
        sec_key_str = dev_data.get("sec_key") or ""
        verify_key_str = dev_data.get("verify_key") or ""
        local_key_b = local_key_str.encode("utf-8") if local_key_str else None
        sec_key_b = sec_key_str.encode("utf-8") if sec_key_str else None
        auth_key_b = bytes.fromhex(auth_key_str) if auth_key_str else None
        auth_random_b = bytes.fromhex(auth_random_str) if auth_random_str else None
        verify_key_b = bytes.fromhex(verify_key_str) if verify_key_str else None

        profile = await async_load_profile(hass, product_id)
        profiles[mac] = profile

        protocol_version = profile.get("protocol_version", 4)
        session = TuyaBLELockSession(
            hass, ble_device, login_key, virtual_id, device_uuid,
            auth_key=auth_key_b,
            auth_random=auth_random_b,
            protocol_version=protocol_version,
            local_key=local_key_b,
            sec_key=sec_key_b,
            verify_key=verify_key_b,
            check_code=dev_data.get("check_code", ""),
        )

        coordinator = TuyaBLELockCoordinator(
            hass, entry, mac, device_name, dev_data,
            ble_device, session, profile,
        )
        # Seed state from last-known cloud DP snapshot so event-only
        # entities (doorbell, hijack, last unlock, door, manual_lock,
        # auto_lock_time…) aren't 'unknown' on startup. BLE pushes will
        # overwrite these as real events occur.
        cloud_dps = dev_data.get("cloud_dps") or {}
        if cloud_dps:
            try:
                coordinator.apply_cloud_dps(cloud_dps)
            except Exception:
                _LOGGER.debug("Cloud DP seed failed for %s", mac, exc_info=True)
        coordinators[mac] = coordinator

        # Watch the lock's advertisement flag so we can connect on demand
        # instead of holding a battery-draining persistent connection.
        # This rides HA's existing proxy feed: no extra BLE client, and so
        # no contention with the connections the coordinator itself makes.
        if profile.get("event_flag"):
            adv_unsubscribes.append(
                bluetooth.async_register_callback(
                    hass,
                    _make_advertisement_handler(coordinator),
                    {"address": mac.upper(), "connectable": False},
                    # Active scanning: the flag is only up for 21-30s and each
                    # advertisement is easily lost at this range, so ask the
                    # scanner to work for them rather than listen passively.
                    bluetooth.BluetoothScanningMode.ACTIVE,
                )
            )
            # HA only calls back when the payload changes, so a flag rise is a
            # single notification. Poll the cached advertisement as well so one
            # lost packet does not silently drop an unlock.
            coordinator.start_advertisement_poll(entry)

        # One-shot status fetch in background
        entry.async_create_background_task(
            hass, coordinator.async_one_shot_status(),
            f"tuya_ble_access_status_{mac}",
        )

    platforms = _platforms_for_devices(profiles)
    runtime_data = TuyaBLELockData(
        device_store=device_store,
        credential_store=credential_store,
        coordinators=coordinators,
        platforms=platforms,
        adv_unsubscribes=adv_unsubscribes,
    )
    entry.runtime_data = runtime_data
    hass.data[DOMAIN]["device_store"] = device_store

    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


def _make_advertisement_handler(coordinator):
    """Bind one coordinator to the bluetooth callback signature."""

    def _handle(service_info, change) -> None:
        coordinator.handle_advertisement(service_info)

    return _handle


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """Let a lock be deleted from the device page, dropping its credentials.

    Without this hook Home Assistant refuses device deletion, so a device that
    should never have been adopted (a Tuya gateway picked up by the FD50
    discovery matcher, for example) could not be removed. The hub device itself
    is not removable — deleting that means deleting the config entry.
    """
    store = (hass.data.get(DOMAIN) or {}).get("device_store")
    macs = {
        identifier[1]
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    }
    if config_entry.entry_id in macs:
        return False
    for mac in macs:
        if store is not None and store.get_device(mac):
            await store.async_remove_device(mac)
            _LOGGER.info("Removed %s from the device store on user request", mac)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data: TuyaBLELockData = entry.runtime_data
    for unsub in data.adv_unsubscribes:
        unsub()
    for coordinator in data.coordinators.values():
        coordinator._stopping = True
        if coordinator._adv_poll_task and not coordinator._adv_poll_task.done():
            coordinator._adv_poll_task.cancel()
        coordinator._persistent_connection = False
        if coordinator._keepalive_task and not coordinator._keepalive_task.done():
            coordinator._keepalive_task.cancel()
        if coordinator._idle_timer is not None:
            coordinator._idle_timer.cancel()
        await coordinator._session.async_disconnect()
    return await hass.config_entries.async_unload_platforms(entry, data.platforms)
