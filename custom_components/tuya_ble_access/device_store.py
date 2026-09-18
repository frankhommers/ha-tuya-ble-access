"""Persistent per-device BLE credential storage.

Each device (lock) has its own entry keyed by MAC address, storing
the BLE credentials needed for communication (login_key, virtual_id, etc.).
This is separate from the hub config entry.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY_DEVICES, STORAGE_VERSION


class DeviceStore:
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DEVICES)
        self._data: dict = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"devices": {}}
        registry = DeviceKeyRegistry(self._hass)
        for mac, credentials in self.devices.items():
            await registry.async_remember(mac, credentials, source="active_device", only_if_missing=True)

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    @property
    def devices(self) -> dict[str, dict]:
        return self._data.get("devices", {})

    def get_device(self, mac: str) -> dict | None:
        return self.devices.get(mac.upper())

    async def async_add_device(self, mac: str, device_data: dict) -> None:
        await DeviceKeyRegistry(self._hass).async_remember(mac, device_data, source="active_device")
        self._data.setdefault("devices", {})[mac.upper()] = device_data
        await self.async_save()

    async def async_update_device(self, mac: str, **kwargs) -> None:
        dev = self.devices.get(mac.upper())
        if dev:
            await DeviceKeyRegistry(self._hass).async_remember(mac, {**dev, **kwargs}, source="active_device")
            dev.update(kwargs)
            await self.async_save()

    async def async_remove_device(self, mac: str) -> None:
        self._data.get("devices", {}).pop(mac.upper(), None)
        await self.async_save()


class ActivationSeedStore:
    """Remember checked activation credentials even when BLE pairing fails.

    Kept separate from active devices: caching keys must not start a coordinator.
    """

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, 1, f"{STORAGE_KEY_DEVICES}_activation")
        self._lock = hass.data.setdefault(DOMAIN, {}).setdefault(
            "activation_seed_storage_lock", asyncio.Lock()
        )

    async def async_get_seed(self, mac: str) -> dict | None:
        async with self._lock:
            data = await self._store.async_load() or {}
            seed = data.get(mac.upper())
        registry = DeviceKeyRegistry(self._hass)
        if seed:
            await registry.async_remember(mac, seed, source="activation", only_if_missing=True)
        latest = await registry.async_latest(mac)
        # Newer cloud records supersede older activation seeds. Never merge keys
        # from different generations or silently retry an older key generation.
        return latest if latest is not None else seed

    async def async_save_seed(self, mac: str, seed: dict) -> None:
        await DeviceKeyRegistry(self._hass).async_remember(mac, seed, source="activation")
        async with self._lock:
            data = await self._store.async_load() or {}
            data[mac.upper()] = seed
            await self._store.async_save(data)


class DeviceKeyRegistry:
    """Append-only local key history, independent of configured HA devices.

    Explicitly selected fields only: never store account passwords or DP payloads.
    Removal of an entity/device does not remove this recovery history.
    """

    FIELDS = frozenset({
        "uuid", "device_id", "product_id", "name", "category", "auth_key",
        "auth_random", "local_key", "sec_key", "verify_key", "check_code",
        "login_key", "virtual_id", "key_error",
    })

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, 1, f"{STORAGE_KEY_DEVICES}_key_history")
        self._lock = hass.data.setdefault(DOMAIN, {}).setdefault(
            "device_key_history_lock", asyncio.Lock()
        )

    async def async_remember(
        self, mac: str, credentials: dict, *, source: str, only_if_missing: bool = False
    ) -> None:
        snapshot = {key: deepcopy(value) for key, value in credentials.items() if key in self.FIELDS}
        if not mac or not snapshot:
            return
        async with self._lock:
            data = await self._store.async_load() or {}
            if only_if_missing and mac.upper() in data:
                return
            record = data.setdefault(mac.upper(), {"history": []})
            history = record["history"]
            now = datetime.now(timezone.utc).isoformat()
            if history and history[-1]["credentials"] == snapshot:
                history[-1]["last_seen"] = now
            else:
                history.append({"first_seen": now, "last_seen": now, "source": source, "credentials": snapshot})
            await self._store.async_save(data)

    async def async_latest(self, mac: str) -> dict | None:
        async with self._lock:
            data = await self._store.async_load() or {}
            history = data.get(mac.upper(), {}).get("history", [])
            if not history:
                return None
            latest = history[-1]
            credentials = latest["credentials"]
            # Unknown/non-lock cloud records are retained, never trusted as seeds.
            if latest["source"] == "cloud":
                from .const import LOCK_CATEGORIES
                if credentials.get("category") not in LOCK_CATEGORIES:
                    return {}
            return deepcopy(credentials)


    async def async_inventory(self) -> dict[str, dict]:
        """Return latest known records without requiring a Bluetooth advertisement."""
        legacy = await Store(self._hass, 1, f"{STORAGE_KEY_DEVICES}_activation").async_load() or {}
        for mac, seed in legacy.items():
            await self.async_remember(mac, seed, source="activation", only_if_missing=True)
        async with self._lock:
            data = await self._store.async_load() or {}
            return {
                mac: deepcopy(record["history"][-1]["credentials"])
                for mac, record in data.items() if record.get("history")
            }
