"""Saved activation keys survive fresh store instances without activating devices."""

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def test_activation_keys_persist_by_mac_and_concurrent_saves_keep_both(monkeypatch):
    disk = {}

    class Store:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            snapshot = deepcopy(disk.get(self.key))
            await asyncio.sleep(0)
            return snapshot

        async def async_save(self, data):
            disk[self.key] = deepcopy(data)

    package = "_activation_store_test"
    package_module = types.ModuleType(package)
    package_module.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package, package_module)
    monkeypatch.setitem(sys.modules, "homeassistant.core", types.SimpleNamespace(HomeAssistant=object))
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", types.SimpleNamespace(Store=Store))
    spec = importlib.util.spec_from_file_location(f"{package}.device_store", ROOT / "device_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def run_test():
        hass = types.SimpleNamespace(data={})
        first = module.ActivationSeedStore(hass)
        second = module.ActivationSeedStore(hass)
        await asyncio.gather(
            first.async_save_seed("aa:bb:cc:dd:ee:01", {"local_key": "first-key"}),
            second.async_save_seed("aa:bb:cc:dd:ee:02", {"local_key": "second-key"}),
        )
        # A fresh HA/store instance reloads the saved data; active devices stay empty.
        restarted_hass = types.SimpleNamespace(data={})
        restarted = module.ActivationSeedStore(restarted_hass)
        assert await restarted.async_get_seed("AA:BB:CC:DD:EE:01") == {"local_key": "first-key"}
        assert await restarted.async_get_seed("aa:bb:cc:dd:ee:02") == {"local_key": "second-key"}
        devices = module.DeviceStore(restarted_hass)
        await devices.async_load()
        assert devices.devices == {}

    asyncio.run(run_test())
