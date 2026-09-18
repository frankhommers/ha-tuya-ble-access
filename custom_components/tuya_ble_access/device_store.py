"""Persistent per-device BLE credential storage.

Each device (lock) has its own entry keyed by MAC address, storing
the BLE credentials needed for communication (login_key, virtual_id, etc.).
This is separate from the hub config entry.
"""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY_DEVICES, STORAGE_VERSION


class DeviceStore:
    def __init__(self, hass: HomeAssistant):
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_DEVICES)
        self._data: dict = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {"devices": {}}

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    @property
    def devices(self) -> dict[str, dict]:
        return self._data.get("devices", {})

    def get_device(self, mac: str) -> dict | None:
        return self.devices.get(mac.upper())

    async def async_add_device(self, mac: str, device_data: dict) -> None:
        self._data.setdefault("devices", {})[mac.upper()] = device_data
        await self.async_save()

    async def async_update_device(self, mac: str, **kwargs) -> None:
        dev = self.devices.get(mac.upper())
        if dev:
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
        self._store = Store(hass, 1, f"{STORAGE_KEY_DEVICES}_activation")
        self._lock = hass.data.setdefault(DOMAIN, {}).setdefault(
            "activation_seed_storage_lock", asyncio.Lock()
        )

    async def async_get_seed(self, mac: str) -> dict | None:
        async with self._lock:
            data = await self._store.async_load() or {}
            return data.get(mac.upper())

    async def async_save_seed(self, mac: str, seed: dict) -> None:
        async with self._lock:
            data = await self._store.async_load() or {}
            data[mac.upper()] = seed
            await self._store.async_save(data)
