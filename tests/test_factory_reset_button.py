"""The reset-report button must remain usable after BLE pairing is lost."""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


@pytest.mark.parametrize("setup_method", ["local", "cloud"])
def test_reset_button_available_offline_and_forwards_context(monkeypatch, setup_method):
    package_name = "_factory_reset_button_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]

    class BaseEntity:
        def __init__(self, coordinator, entry):
            self.coordinator = coordinator
            self._entry = entry
            self._mac = coordinator.mac

        @property
        def available(self):
            return self.coordinator.last_update_success

    replacements = {
        package_name: package,
        "homeassistant.components.button": types.SimpleNamespace(ButtonEntity=type("ButtonEntity", (), {})),
        "homeassistant.const": types.SimpleNamespace(EntityCategory=types.SimpleNamespace(CONFIG="config")),
        "homeassistant.exceptions": types.SimpleNamespace(HomeAssistantError=RuntimeError),
        f"{package_name}.const": types.SimpleNamespace(
            CONF_SETUP_METHOD="setup_method", DOMAIN="tuya_ble_access", SETUP_METHOD_CLOUD="cloud",
        ),
        f"{package_name}.entity": types.SimpleNamespace(TuyaBLELockEntity=BaseEntity),
        f"{package_name}.models": types.SimpleNamespace(TuyaBLELockData=object),
    }
    for name, value in replacements.items():
        monkeypatch.setitem(sys.modules, name, value)
    spec = importlib.util.spec_from_file_location(f"{package_name}.button", ROOT / "button.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def run():
        mac = "AA:BB:CC:DD:EE:FF"
        coordinator = types.SimpleNamespace(mac=mac, last_update_success=False)
        entry = types.SimpleNamespace(
            data={"setup_method": setup_method},
            runtime_data=types.SimpleNamespace(coordinators={mac: coordinator}),
        )
        calls = []

        async def call(*args, **kwargs):
            calls.append((args, kwargs))

        hass = types.SimpleNamespace(services=types.SimpleNamespace(async_call=call))
        entities = []
        await module.async_setup_entry(hass, entry, entities.extend)
        buttons = [e for e in entities if isinstance(e, module.TuyaBLEReportFactoryResetButton)]
        assert len(buttons) == 1
        button = buttons[0]
        assert button.available is True
        assert button.unique_id == f"{mac}_report_factory_reset"
        button.hass = hass
        button._context = object()
        await button.async_press()
        assert calls == [(
            ("tuya_ble_access", "report_factory_reset", {"device_id": mac}),
            {"blocking": True, "context": button._context},
        )]
        for filename in ("strings.json", "translations/en.json", "translations/nl.json"):
            translations = json.loads((ROOT / filename).read_text())
            assert translations["entity"]["button"][button._attr_translation_key]["name"]

    asyncio.run(run())
