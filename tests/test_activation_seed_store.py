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
        # Replacement preserves each complete generation, and identical reads
        # don't grow the history. Removing an active device preserves recovery.
        registry = module.DeviceKeyRegistry(restarted_hass)
        old = {"local_key": "old", "category": "jtmspro", "product_id": "ba2qk177"}
        new = {**old, "local_key": "new"}
        await registry.async_remember("AA:BB:CC:DD:EE:01", old, source="cloud")
        await registry.async_remember("AA:BB:CC:DD:EE:01", new, source="cloud")
        await registry.async_remember("AA:BB:CC:DD:EE:01", new, source="cloud")
        history_key = next(key for key in disk if key.endswith("_key_history"))
        history = disk[history_key]["AA:BB:CC:DD:EE:01"]["history"]
        assert [h["credentials"]["local_key"] for h in history] == ["first-key", "old", "new"]
        await devices.async_remove_device("AA:BB:CC:DD:EE:01")
        assert await restarted.async_get_seed("AA:BB:CC:DD:EE:01") == new
        assert await restarted.async_get_seed("AA:BB:CC:DD:EE:02") == {"local_key": "second-key"}
        await registry.async_remember("AA:BB:CC:DD:EE:03", {**new, "category": "wg2", "password": "must-not-persist", "dps": {"71": "private"}}, source="cloud")
        assert await registry.async_latest("AA:BB:CC:DD:EE:03") == {}
        record = disk[history_key]["AA:BB:CC:DD:EE:03"]["history"][0]["credentials"]
        assert "password" not in record and "dps" not in record
        # An incomplete newer generation stays incomplete: no mixing with old keys.
        await registry.async_remember("AA:BB:CC:DD:EE:01", {"category": "jtmspro", "local_key": "partial"}, source="cloud")
        assert await restarted.async_get_seed("AA:BB:CC:DD:EE:01") == {"category": "jtmspro", "local_key": "partial"}


    asyncio.run(run_test())
