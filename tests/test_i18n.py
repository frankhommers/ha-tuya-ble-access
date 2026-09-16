"""Translation coverage and stable protocol values for localized UI controls."""

import ast
import asyncio
import importlib.util
import json
import re
import string
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"
EN = json.loads((ROOT / "strings.json").read_text())


def _flatten(data, prefix=""):
    result = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


def _placeholders(value):
    return {field for _, field, _, _ in string.Formatter().parse(value) if field}


@pytest.mark.parametrize("language", ["en", "nl"])
def test_catalogs_are_complete_and_preserve_placeholders(language):
    source = _flatten(EN)
    translated = json.loads((ROOT / f"translations/{language}.json").read_text())
    actual = _flatten(translated)
    assert actual.keys() == source.keys()
    for key, value in actual.items():
        assert isinstance(value, str) and value.strip(), key
        assert _placeholders(value) == _placeholders(source[key]), key
        assert "[%key:" not in value  # Custom integrations need resolved strings.
    if language == "en":
        assert translated == EN


def test_service_names_fields_and_selector_options_are_translated():
    services = yaml.safe_load((ROOT / "services.yaml").read_text())
    assert services.keys() == EN["services"].keys()
    for key, service in services.items():
        translated = EN["services"][key]
        for field in ("name", "description"):
            assert service[field].strip() == translated[field], key
        assert service["fields"].keys() == translated["fields"].keys()
        for field, cfg in service["fields"].items():
            for text in ("name", "description"):
                assert cfg[text].strip() == translated["fields"][field][text], (key, field)
            select = cfg.get("selector", {}).get("select")
            if select:
                options = EN["selector"][select["translation_key"]]["options"]
                assert set(select["options"]) == options.keys()


def test_entity_names_and_errors_use_existing_translation_keys():
    time_tree = ast.parse((ROOT / "credential_time.py").read_text())
    time_error_keys = {
        ast.literal_eval(node.args[0]) for node in ast.walk(time_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "CredentialTimeError"
    }
    assert time_error_keys
    for key in time_error_keys:
        assert not _placeholders(EN["exceptions"][key]["message"])
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = getattr(target, "id", getattr(target, "attr", ""))
                    if name == "_attr_name":
                        assert isinstance(node.value, ast.Constant) and node.value.value is None, path.name
                    if name == "_attr_translation_key" and isinstance(node.value, ast.Constant):
                        assert node.value.value in EN["entity"][path.stem], path.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HomeAssistantError":
                kwargs = {arg.arg: arg.value for arg in node.keywords}
                assert not node.args, (path.name, node.lineno)
                key_node = kwargs["translation_key"]
                if isinstance(key_node, ast.Attribute):
                    # Time validation passes one of the checked keys above.
                    assert ast.unparse(key_node) == "err.translation_key"
                    assert any(
                        isinstance(handler, ast.ExceptHandler)
                        and isinstance(handler.type, ast.Name)
                        and handler.type.id == "CredentialTimeError"
                        and handler.name == "err" and node in ast.walk(handler)
                        for handler in ast.walk(tree)
                    )
                    assert "translation_placeholders" not in kwargs
                    continue
                key = ast.literal_eval(key_node)
                expected = _placeholders(EN["exceptions"][key]["message"])
                placeholders = kwargs.get("translation_placeholders")
                actual = {ast.literal_eval(k) for k in placeholders.keys} if placeholders else set()
                assert actual == expected, key


def _load_select(monkeypatch):
    package = "_tuya_select_i18n_test"

    class Entity:
        def __init__(self, coordinator, entry):
            self.coordinator = coordinator
            self._mac = coordinator.mac

        async def async_added_to_hass(self):
            pass

    replacements = {
        package: types.SimpleNamespace(__path__=[str(ROOT)]),
        f"{package}.entity": types.SimpleNamespace(TuyaBLELockEntity=Entity),
        f"{package}.models": types.SimpleNamespace(TuyaBLELockData=object),
        "homeassistant.components.select": types.SimpleNamespace(SelectEntity=type("SelectEntity", (), {})),
        "homeassistant.const": types.SimpleNamespace(EntityCategory=types.SimpleNamespace(CONFIG="config")),
        "homeassistant.helpers.restore_state": types.SimpleNamespace(RestoreEntity=type("RestoreEntity", (), {})),
    }
    for key, value in replacements.items():
        monkeypatch.setitem(sys.modules, key, value)
    spec = importlib.util.spec_from_file_location(f"{package}.select", ROOT / "select.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selects_use_protocol_values_and_restore_old_english_labels(monkeypatch):
    module = _load_select(monkeypatch)

    async def run():
        calls = []

        async def set_volume(value):
            calls.append(("volume", value))

        async def set_enum(dp, value, key):
            calls.append((dp, value, key))

        for path in (ROOT / "device_profiles").glob("*.json"):
            profile = json.loads(path.read_text())
            if not isinstance(profile, dict):
                continue
            coordinator = types.SimpleNamespace(
                mac="MAC_A", state={}, profile=profile,
                async_set_volume=set_volume, async_set_enum_dp=set_enum,
            )
            entities = []
            entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(coordinators={"MAC_A": coordinator}))
            await module.async_setup_entry(None, entry, entities.extend)
            for entity in entities:
                translated = EN["entity"]["select"][entity._attr_translation_key]["state"]
                for index, value in enumerate(entity._attr_options):
                    assert re.fullmatch(r"[a-z0-9_]+", value)
                    assert value in translated
                    old_label = value.replace("_", " ").capitalize()

                    async def last_state(label=old_label):
                        return types.SimpleNamespace(state=label)

                    coordinator.state.clear()
                    entity.async_get_last_state = last_state
                    await entity.async_added_to_hass()
                    assert entity.current_option == value
                    await entity.async_select_option(value)
                    if entity._attr_translation_key == "volume":
                        assert calls[-1] == ("volume", index)
                    else:
                        assert calls[-1] == (entity._dp, index, entity._state_key)

    asyncio.run(run())
