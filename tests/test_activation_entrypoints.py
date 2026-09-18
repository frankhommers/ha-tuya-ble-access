"""Offline tests for the shared Home Assistant activation orchestrator."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"
PACKAGE = "_activation_pkg"
_MISSING = object()


class _SessionDeviceAlreadyBoundError(Exception):
    pass


class _SessionPairingFailedError(Exception):
    pass


class _SessionBindVerificationError(Exception):
    pass


class _FakeAbortFlow(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _FakeInvalid(Exception):
    pass


class _SchemaKey:
    def __init__(self, key, *, required, default=_MISSING):
        self.key = key
        self.required = required
        self.default = default


class _All:
    def __init__(self, *validators):
        self.validators = validators

    def __call__(self, value):
        for validator in self.validators:
            value = _validate_schema_value(validator, value)
        return value


def _validate_schema_value(validator, value):
    if isinstance(validator, type):
        if not isinstance(value, validator):
            raise _FakeInvalid(f"expected {validator.__name__}")
        return value
    if callable(validator):
        return validator(value)
    if value != validator:
        raise _FakeInvalid(f"expected {validator!r}")
    return value


class _Schema:
    def __init__(self, schema):
        self.schema = schema

    def __call__(self, data):
        result = dict(data)
        for raw_key, validator in self.schema.items():
            key = raw_key.key if isinstance(raw_key, _SchemaKey) else raw_key
            if key not in result:
                if isinstance(raw_key, _SchemaKey) and raw_key.default is not _MISSING:
                    result[key] = raw_key.default
                elif isinstance(raw_key, _SchemaKey) and raw_key.required:
                    raise _FakeInvalid(f"required key not provided: {key}")
                continue
            result[key] = _validate_schema_value(validator, result[key])
        return result


_STUBBED_MODULES = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.bluetooth",
    "homeassistant.core",
    PACKAGE,
    f"{PACKAGE}.activation",
    f"{PACKAGE}.ble_session",
    f"{PACKAGE}.const",
    f"{PACKAGE}.device_store",
    f"{PACKAGE}.tuya_cloud",
)


class _FakeConfigFlowBase:
    def __init_subclass__(cls, **_kwargs):
        super().__init_subclass__()

    def _async_current_entries(self):
        return self.current_entries

    async def async_set_unique_id(self, unique_id):
        self.unique_id_calls.append(unique_id)
        if unique_id in self.in_progress_unique_ids:
            raise _FakeAbortFlow("already_in_progress")
        self.in_progress_unique_ids.add(unique_id)
        return None

    def _abort_if_unique_id_configured(self):
        self.abort_if_configured_calls += 1
        return None

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}

    def async_show_form(
        self,
        *,
        step_id,
        data_schema=None,
        errors=None,
        description_placeholders=None,
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders or {},
        }

    def async_show_menu(self, *, step_id, menu_options):
        return {
            "type": "menu",
            "step_id": step_id,
            "menu_options": menu_options,
        }

    def async_create_entry(self, *, title, data, options=None):
        result = {"type": "create_entry", "title": title, "data": data}
        if options is not None:
            result["options"] = options
        return result


def _load_config_flow_module():
    stubbed_modules = (
        "voluptuous",
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.bluetooth",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.helpers",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.selector",
        PACKAGE,
        f"{PACKAGE}.activation",
        f"{PACKAGE}.config_flow",
        f"{PACKAGE}.const",
        f"{PACKAGE}.device_profiles",
        f"{PACKAGE}.device_store",
        f"{PACKAGE}.tuya_cloud",
    )
    previous_modules = {
        name: sys.modules.get(name, _MISSING) for name in stubbed_modules
    }

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda value: value
    voluptuous.Required = lambda key, **_kwargs: key
    voluptuous.In = lambda choices: choices
    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlow = _FakeConfigFlowBase
    const = types.ModuleType("homeassistant.const")
    const.CONF_EMAIL = "email"
    const.CONF_NAME = "name"
    const.CONF_PASSWORD = "password"
    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.format_mac = lambda address: address.replace("-", ":").lower()
    selector = types.ModuleType("homeassistant.helpers.selector")

    class TextSelectorType:
        PASSWORD = "password"

    class TextSelectorConfig:
        def __init__(self, **kwargs):
            self.config = kwargs

    class TextSelector:
        def __init__(self, config):
            self.config = config

    class SelectSelector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            if value not in self.config["options"]:
                raise _FakeInvalid("invalid option")
            return value

    selector.SelectSelector = SelectSelector
    selector.SelectSelectorConfig = dict
    selector.TextSelector = TextSelector
    selector.TextSelectorConfig = TextSelectorConfig
    selector.TextSelectorType = TextSelectorType
    components.bluetooth = bluetooth
    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.const = const
    homeassistant.helpers = helpers
    helpers.device_registry = device_registry
    helpers.selector = selector

    integration_const = types.ModuleType(f"{PACKAGE}.const")
    integration_const.DOMAIN = "tuya_ble_access"
    integration_const.CONF_TUYA_EMAIL = "tuya_email"
    integration_const.CONF_TUYA_PASSWORD = "tuya_password"
    integration_const.CONF_TUYA_COUNTRY = "tuya_country_code"
    integration_const.CONF_TUYA_REGION = "tuya_region"
    integration_const.CONF_SETUP_METHOD = "setup_method"
    integration_const.SETUP_METHOD_CLOUD = "cloud"
    integration_const.SETUP_METHOD_LOCAL = "local"
    integration_const.LOCK_CATEGORIES = frozenset({"jtmspro"})
    integration_const.CONF_AUTH_KEY = "auth_key"
    integration_const.CONF_AUTH_RANDOM = "auth_random"
    integration_const.CONF_CHECK_CODE = "check_code"
    integration_const.CONF_DEVICE_MAC = "device_mac"
    integration_const.CONF_DEVICE_UUID = "device_uuid"
    integration_const.CONF_LOCAL_KEY = "local_key"
    integration_const.CONF_PRODUCT_ID = "product_id"
    integration_const.CONF_SEC_KEY = "sec_key"
    integration_const.CONF_VERIFY_KEY = "verify_key"
    device_profiles = types.ModuleType(f"{PACKAGE}.device_profiles")
    device_profiles.async_get_profile_choices = None
    # Exercise the real classification helper against bundled product profiles.
    profile_spec = importlib.util.spec_from_file_location("_test_profiles", ROOT / "device_profiles" / "__init__.py")
    profile_module = importlib.util.module_from_spec(profile_spec)
    profile_spec.loader.exec_module(profile_module)
    device_profiles.async_resolve_category = profile_module.async_resolve_category
    device_store = types.ModuleType(f"{PACKAGE}.device_store")
    device_store.DeviceStore = object
    device_store.ActivationSeedStore = object
    device_store.DeviceKeyRegistry = object
    tuya_cloud = types.ModuleType(f"{PACKAGE}.tuya_cloud")
    tuya_cloud.async_fetch_auth_key = None
    tuya_cloud.async_sync_cloud_inventory = None
    activation_stub = types.ModuleType(f"{PACKAGE}.activation")

    class ActivationError(Exception):
        pass

    class MissingActivationSeedError(ActivationError):
        pass

    class BluetoothUnavailableError(ActivationError):
        pass

    class DeviceAlreadyBoundActivationError(ActivationError):
        pass

    class PairingFailedActivationError(ActivationError):
        pass

    class BindVerificationActivationError(ActivationError):
        pass

    class CloudFetchActivationError(ActivationError):
        pass

    class StorageUnavailableActivationError(ActivationError):
        pass

    class PostBindPersistenceActivationError(ActivationError):
        pass

    activation_stub.ActivationError = ActivationError
    activation_stub.MissingActivationSeedError = activation.MissingActivationSeedError
    activation_stub.validate_activation_seed = activation.validate_activation_seed
    activation_stub.BluetoothUnavailableError = BluetoothUnavailableError
    activation_stub.DeviceAlreadyBoundActivationError = DeviceAlreadyBoundActivationError
    activation_stub.PairingFailedActivationError = PairingFailedActivationError
    activation_stub.BindVerificationActivationError = BindVerificationActivationError
    activation_stub.CloudFetchActivationError = CloudFetchActivationError
    activation_stub.StorageUnavailableActivationError = StorageUnavailableActivationError
    activation_stub.PostBindPersistenceActivationError = (
        PostBindPersistenceActivationError
    )
    activation_stub.async_activate_lock = None

    replacements = {
        "voluptuous": voluptuous,
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.bluetooth": bluetooth,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.selector": selector,
        PACKAGE: package,
        integration_const.__name__: integration_const,
        device_profiles.__name__: device_profiles,
        device_store.__name__: device_store,
        tuya_cloud.__name__: tuya_cloud,
        activation_stub.__name__: activation_stub,
    }
    sys.modules.update(replacements)

    name = f"{PACKAGE}.config_flow"
    spec = importlib.util.spec_from_file_location(name, ROOT / "config_flow.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module, activation_stub
    finally:
        for module_name, previous in previous_modules.items():
            if previous is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


def _load_activation_module():
    previous_modules = {
        name: sys.modules.get(name, _MISSING) for name in _STUBBED_MODULES
    }
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    bluetooth.async_ble_device_from_address = lambda *_args, **_kwargs: None
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    components.bluetooth = bluetooth
    homeassistant.components = components
    homeassistant.core = core
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.bluetooth"] = bluetooth
    sys.modules["homeassistant.core"] = core
    sys.modules[PACKAGE] = package

    device_store = types.ModuleType(f"{PACKAGE}.device_store")
    device_store.DeviceStore = object
    ble_session = types.ModuleType(f"{PACKAGE}.ble_session")
    ble_session.TuyaBLELockSession = object
    ble_session.DeviceAlreadyBoundError = _SessionDeviceAlreadyBoundError
    ble_session.PairingFailedError = _SessionPairingFailedError
    ble_session.BindVerificationError = _SessionBindVerificationError
    tuya_cloud = types.ModuleType(f"{PACKAGE}.tuya_cloud")
    tuya_cloud.async_fetch_auth_key = None
    sys.modules[device_store.__name__] = device_store
    sys.modules[ble_session.__name__] = ble_session
    sys.modules[tuya_cloud.__name__] = tuya_cloud

    name = f"{PACKAGE}.activation"
    spec = importlib.util.spec_from_file_location(name, ROOT / "activation.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for module_name, previous in previous_modules.items():
            if previous is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


def _load_services_module():
    stubbed_modules = (
        "voluptuous",
        "homeassistant",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.service",
        PACKAGE,
        f"{PACKAGE}.activation",
        f"{PACKAGE}.ble_commands",
        f"{PACKAGE}.const",
        f"{PACKAGE}.credential_store",
        f"{PACKAGE}.models",
        f"{PACKAGE}.services",
    )
    previous_modules = {
        name: sys.modules.get(name, _MISSING) for name in stubbed_modules
    }

    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Invalid = _FakeInvalid
    voluptuous.Schema = _Schema
    voluptuous.Required = lambda key, **kwargs: _SchemaKey(
        key, required=True, default=kwargs.get("default", _MISSING)
    )
    voluptuous.Optional = lambda key, **kwargs: _SchemaKey(
        key, required=False, default=kwargs.get("default", _MISSING)
    )
    voluptuous.All = _All
    voluptuous.Any = lambda *validators: validators[0]

    def validate_in(choices):
        def validate(value):
            if value not in choices:
                raise _FakeInvalid(f"expected one of {choices!r}")
            return value
        return validate

    voluptuous.In = validate_in

    homeassistant = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")

    class SupportsResponse:
        OPTIONAL = "optional"
        ONLY = "only"

    core.HomeAssistant = object
    core.ServiceCall = types.SimpleNamespace
    core.SupportsResponse = SupportsResponse
    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        def __init__(self, *args, translation_domain=None, translation_key=None,
                     translation_placeholders=None):
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders or {}
            if not args and translation_key:
                catalog = json.loads((ROOT / "translations/en.json").read_text())
                args = (catalog["exceptions"][translation_key]["message"].format(
                    **self.translation_placeholders
                ),)
            super().__init__(*args)

    exceptions.HomeAssistantError = HomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    service_helpers = types.ModuleType("homeassistant.helpers.service")

    def async_register_admin_service(
        hass,
        domain,
        service,
        handler,
        schema=None,
        supports_response=None,
        **_kwargs,
    ):
        hass.admin_service_registrations.append(
            (domain, service, handler, schema, supports_response)
        )
        hass.services.register_admin_service(
            domain,
            service,
            handler,
            schema=schema,
            supports_response=supports_response,
        )

    def async_get_config_entry(hass, domain, entry_id):
        hass.config_entry_service_calls.append((domain, entry_id))
        if entry_id is not None:
            raise AssertionError("activate must request the domain's single entry")
        entries = hass.config_entries.async_entries(
            domain, include_ignore=False, include_disabled=False
        )
        if not entries:
            raise HomeAssistantError("No active Tuya BLE Access hub is configured")
        if len(entries) > 1:
            raise HomeAssistantError(
                "Multiple active Tuya BLE Access hubs are configured; activation is ambiguous"
            )
        entry = entries[0]
        if entry.state != "loaded":
            raise HomeAssistantError("The Tuya BLE Access hub is not loaded")
        return entry

    service_helpers.async_register_admin_service = async_register_admin_service
    service_helpers.async_get_config_entry = async_get_config_entry
    helpers.service = service_helpers
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers

    integration_const = types.ModuleType(f"{PACKAGE}.const")
    integration_const.DOMAIN = "tuya_ble_access"
    integration_const.CRED_PASSWORD = 1
    integration_const.CRED_FINGERPRINT = 2
    integration_const.CRED_CARD = 3
    integration_const.STAGE_NAMES = {}
    credential_store = types.ModuleType(f"{PACKAGE}.credential_store")
    credential_store.CredentialStore = object
    ble_commands = types.ModuleType(f"{PACKAGE}.ble_commands")
    ble_commands.build_enroll_payload = None
    ble_commands.build_delete_payload = None
    ble_commands.build_temp_password_payload = None
    ble_commands.parse_temp_password_response = None
    ble_commands.parse_enroll_response = None
    ble_commands.parse_sync_bitmap = None
    ble_commands.parse_credential_list = None
    ble_commands.SYNC_MARKER = b""
    models = types.ModuleType(f"{PACKAGE}.models")
    models.TuyaBLELockData = object
    activation_stub = types.ModuleType(f"{PACKAGE}.activation")

    class ActivationError(Exception):
        pass

    activation_stub.ActivationError = ActivationError
    activation_stub.async_activate_lock = None

    replacements = {
        "voluptuous": voluptuous,
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.service": service_helpers,
        PACKAGE: package,
        integration_const.__name__: integration_const,
        credential_store.__name__: credential_store,
        ble_commands.__name__: ble_commands,
        models.__name__: models,
        activation_stub.__name__: activation_stub,
    }
    sys.modules.update(replacements)

    name = f"{PACKAGE}.services"
    spec = importlib.util.spec_from_file_location(name, ROOT / "services.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module, activation_stub, HomeAssistantError, SupportsResponse
    finally:
        for module_name, previous in previous_modules.items():
            if previous is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous


activation = _load_activation_module()
config_flow, config_flow_activation = _load_config_flow_module()
services, services_activation, ServiceHomeAssistantError, ServiceSupportsResponse = (
    _load_services_module()
)


class FakeConfigEntries:
    def async_entries(self, domain=None):
        return []

    def __init__(self, events: list[str]):
        self.events = events
        self.reloads: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        self.events.append("reload")
        self.reloads.append(entry_id)


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def __init__(self, events: list[str]):
        self.data = {}
        self.config_entries = FakeConfigEntries(events)


class FakeServiceRegistry:
    def __init__(self):
        self.registrations = []
        self.plain_registrations = []

    def has_service(self, domain, service):
        return any(
            registration[0:2] == (domain, service)
            for registration in self.registrations
        )

    def async_register(
        self,
        domain,
        service,
        handler,
        *,
        schema=None,
        supports_response=None,
    ):
        self.plain_registrations.append((domain, service))
        self.registrations.append(
            (domain, service, handler, schema, supports_response)
        )

    def register_admin_service(
        self,
        domain,
        service,
        handler,
        *,
        schema=None,
        supports_response=None,
    ):
        self.registrations.append(
            (domain, service, handler, schema, supports_response)
        )

    def registration(self, domain, service):
        return next(
            registration
            for registration in self.registrations
            if registration[0:2] == (domain, service)
        )


class FakeServiceConfigEntries:
    def __init__(self, entries):
        self.entries = entries

    def async_entries(
        self, domain, *, include_ignore=True, include_disabled=True
    ):
        assert domain == "tuya_ble_access"
        return [
            entry
            for entry in self.entries
            if (include_ignore or entry.source != "ignore")
            and (include_disabled or entry.disabled_by is None)
        ]


class FakeServiceHass:
    def __init__(self, entries):
        self.services = FakeServiceRegistry()
        self.config_entries = FakeServiceConfigEntries(entries)
        self.admin_service_registrations = []
        self.config_entry_service_calls = []
        self.data = {}
        self.states = types.SimpleNamespace(get=lambda _entity_id: None)


async def _activate_service_registration(hass):
    await services.async_register_services(hass)
    return hass.services.registration("tuya_ble_access", "activate")


class FakeStore:
    def __init__(self, events: list[str]):
        self.events = events
        self.devices: dict[str, dict] = {}
        self.activation_seeds = {}

    async def async_load(self) -> None:
        self.events.append("load")

    async def async_add_device(self, address: str, record: dict) -> None:
        self.events.append("persist")
        self.devices[address.upper()] = record

    def get_device(self, address: str):
        return self.devices.get(address.upper())


class DisconnectableSession:
    async def async_disconnect(self):
        pass


def _entry(
    *,
    entry_id="hub-entry",
    password="secret",
    state="loaded",
    disabled_by=None,
    source="user",
):
    return types.SimpleNamespace(
        entry_id=entry_id,
        domain="tuya_ble_access",
        title="Tuya BLE Access",
        state=state,
        disabled_by=disabled_by,
        source=source,
        data={
            "tuya_email": "user@example.com",
            "tuya_password": password,
            "tuya_country_code": "31",
            "tuya_region": "eu",
        },
    )


def _seed(**updates):
    seed = {
        "encryptedAuthKey": "00" * 16,
        "random": "11" * 16,
        "local_key": "abcdefghijklmnop",
        "sec_key": "ponmlkjihgfedcba",
        "verify_key": "aabbccdd",
        "device_id": "device-id",
        "uuid": "uuidc064f275f947",
        "product_id": "product-id",
        "name": "Cloud Lock",
        "check_code": "12345678",
        "category": "jtmspro",
        "dps": {"8": 87},
    }
    seed.update(updates)
    return seed


def _discovery(
    name: str,
    *,
    address: str = "AA:BB:CC:DD:EE:FF",
    with_encrypted_uuid: bool = False,
):
    return types.SimpleNamespace(
        address=address,
        name=name,
        service_data=(
            {"0000fd50-0000-1000-8000-00805f9b34fb": b"service-data"}
            if with_encrypted_uuid
            else {}
        ),
        manufacturer_data=(
            {0x07D0: bytes(range(20))} if with_encrypted_uuid else {}
        ),
    )


async def _discover_and_check(flow, discovery):
    """Follow discovery with the user's explicit credential-check click."""
    result = await flow.async_step_bluetooth(discovery)
    if result.get("step_id") == "check_device":
        return await flow.async_step_check_device({})
    if result.get("step_id") == "sync_cloud":
        result = await flow.async_step_sync_cloud({})
        if result.get("step_id") == "saved_locks":
            result = await flow.async_step_saved_locks({"device_mac": flow._mac})
    return result


def _new_config_flow(
    monkeypatch, hass, entry, store, *, in_progress_unique_ids=None
):
    flow = config_flow.TuyaBLELockConfigFlow()
    flow.hass = hass
    if not hasattr(store, "inventory"):
        store.inventory = {}
    flow.current_entries = [entry]
    flow.unique_id_calls = []
    flow.abort_if_configured_calls = 0
    flow.in_progress_unique_ids = (
        in_progress_unique_ids if in_progress_unique_ids is not None else set()
    )
    monkeypatch.setattr(config_flow, "DeviceStore", lambda _hass: store)

    class Seeds:
        async def async_get_seed(self, mac):
            return store.inventory.get(mac.upper()) or store.activation_seeds.get(mac.upper())

        async def async_save_seed(self, mac, seed):
            store.activation_seeds[mac.upper()] = seed

    monkeypatch.setattr(config_flow, "ActivationSeedStore", lambda _hass: Seeds())

    class Registry:
        async def async_inventory(self):
            return store.inventory
    monkeypatch.setattr(config_flow, "DeviceKeyRegistry", lambda _hass: Registry())

    async def default_sync(hass, email, password, country, region):
        data = await config_flow.async_fetch_auth_key(hass, flow._uuid or "", email, password, country, region, device_mac=flow._mac or "")
        if data.get("device_id"):
            store.inventory[(flow._mac or "AA:BB:CC:DD:EE:FF").upper()] = data
        return store.inventory
    monkeypatch.setattr(config_flow, "async_sync_cloud_inventory", default_sync)


    async def default_cloud_fetch(*_args, **_kwargs):
        return _seed()

    monkeypatch.setattr(config_flow, "async_fetch_auth_key", default_cloud_fetch)
    return flow


def _install_common_fakes(
    monkeypatch,
    hass,
    store,
    seed,
    events,
    requested_uuid="discovered-uuid",
):
    async def fake_fetch(
        passed_hass,
        device_uuid,
        email,
        password,
        country,
        region,
        *,
        device_mac,
    ):
        assert passed_hass is hass
        assert (device_uuid, device_mac) == (
            requested_uuid,
            "AA:BB:CC:DD:EE:FF",
        )
        assert (email, password, country, region) == (
            "user@example.com",
            "secret",
            "31",
            "eu",
        )
        events.append("fetch")
        return seed

    ble_device = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

    def fake_ble_lookup(passed_hass, address, *, connectable):
        assert passed_hass is hass
        assert address == "AA:BB:CC:DD:EE:FF"
        assert connectable is True
        events.append("ble")
        return ble_device

    monkeypatch.setattr(activation, "async_fetch_auth_key", fake_fetch)
    monkeypatch.setattr(activation, "DeviceStore", lambda _hass: store)
    monkeypatch.setattr(
        activation.bluetooth, "async_ble_device_from_address", fake_ble_lookup
    )
    return ble_device


def test_activation_persists_only_after_verified_bind(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        ble_device = _install_common_fakes(
            monkeypatch, hass, store, _seed(), events
        )
        expected_login_key = b"abcdef"
        expected_virtual_id = (b"device-id" + b"\x00" * 22)[:22]

        class FakeSession(DisconnectableSession):
            def __init__(
                self,
                passed_hass,
                passed_ble_device,
                login_key,
                virtual_id,
                device_uuid,
                **kwargs,
            ):
                assert passed_hass is hass
                assert passed_ble_device is ble_device
                assert login_key == expected_login_key
                assert virtual_id == expected_virtual_id
                assert device_uuid == "uuidc064f275f947"
                assert kwargs == {
                    "auth_key": bytes.fromhex("00" * 16),
                    "auth_random": bytes.fromhex("11" * 16),
                    "local_key": b"abcdefghijklmnop",
                    "sec_key": b"ponmlkjihgfedcba",
                    "verify_key": bytes.fromhex("aabbccdd"),
                    "check_code": "12345678",
                }

            async def async_pair_first_activation(self, auth_key_hex):
                assert auth_key_hex == "00" * 16
                assert store.devices == {}
                events.append("verified")
                return expected_login_key, expected_virtual_id

            async def async_disconnect(self):
                events.append("disconnect")

        monkeypatch.setattr(activation, "TuyaBLELockSession", FakeSession)

        result = await activation.async_activate_lock(
            hass,
            _entry(),
            address="AA:BB:CC:DD:EE:FF",
            device_uuid="discovered-uuid",
            name="Discovered Lock",
        )

        expected_record = {
            "uuid": "uuidc064f275f947",
            "login_key": expected_login_key.hex(),
            "virtual_id": expected_virtual_id.hex(),
            "auth_key": "00" * 16,
            "auth_random": "11" * 16,
            "product_id": "product-id",
            "name": "Discovered Lock",
            "local_key": "abcdefghijklmnop",
            "sec_key": "ponmlkjihgfedcba",
            "verify_key": "aabbccdd",
            "check_code": "12345678",
            "cloud_dps": {"8": 87},
        }
        assert result == expected_record
        assert store.devices == {"AA:BB:CC:DD:EE:FF": expected_record}
        assert (
            events.index("verified")
            < events.index("disconnect")
            < events.index("persist")
            < events.index("reload")
        )
        assert hass.config_entries.reloads == ["hub-entry"]

    asyncio.run(run_test())


def test_activation_failure_leaves_store_unchanged(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)

        class FailingSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                raise RuntimeError("sec-14 bind verification failed")

            async def async_disconnect(self):
                events.append("disconnect")
                raise RuntimeError("disconnect also failed")

        monkeypatch.setattr(activation, "TuyaBLELockSession", FailingSession)

        try:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        except RuntimeError as exc:
            assert str(exc) == "sec-14 bind verification failed"
        else:
            raise AssertionError("activation failure was not propagated")

        assert store.devices == {}
        assert events.count("disconnect") == 1
        assert "persist" not in events
        assert hass.config_entries.reloads == []

    asyncio.run(run_test())


def test_activation_maps_cloud_fetch_failure(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        cloud_error = OSError("sensitive cloud failure")

        async def failing_fetch(*_args, **_kwargs):
            raise cloud_error

        monkeypatch.setattr(activation, "async_fetch_auth_key", failing_fetch)

        with pytest.raises(Exception) as exc_info:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )

        assert type(exc_info.value).__name__ == "CloudFetchActivationError"
        assert exc_info.value.__cause__ is cloud_error
        assert "sensitive" not in str(exc_info.value)
        assert events == []

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("session_error", "expected_error_name"),
    [
        (
            _SessionDeviceAlreadyBoundError("sensitive bound details"),
            "DeviceAlreadyBoundActivationError",
        ),
        (
            _SessionPairingFailedError("sensitive PAIR details"),
            "PairingFailedActivationError",
        ),
        (
            _SessionBindVerificationError("sensitive bind details"),
            "BindVerificationActivationError",
        ),
    ],
)
def test_activation_maps_typed_session_failures(
    monkeypatch, session_error, expected_error_name
):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)

        class FailingSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                raise session_error

            async def async_disconnect(self):
                events.append("disconnect")

        monkeypatch.setattr(activation, "TuyaBLELockSession", FailingSession)

        try:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        except Exception as exc:
            assert type(exc).__name__ == expected_error_name
            assert exc.__cause__ is session_error
            assert "sensitive" not in str(exc)
        else:
            raise AssertionError("typed session failure was not propagated")

        assert events.count("disconnect") == 1
        assert store.devices == {}
        assert hass.config_entries.reloads == []

    asyncio.run(run_test())


def test_activation_disconnects_when_cancelled_without_masking_cancellation(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)
        pairing_started = asyncio.Event()

        class CancelledSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                pairing_started.set()
                await asyncio.Event().wait()

            async def async_disconnect(self):
                events.append("disconnect")
                raise RuntimeError("disconnect also failed")

        monkeypatch.setattr(activation, "TuyaBLELockSession", CancelledSession)

        task = asyncio.create_task(
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        )
        await pairing_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert events.count("disconnect") == 1
        assert store.devices == {}

    asyncio.run(run_test())


def test_activation_checks_storage_before_binding(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)

        storage_error = OSError("sensitive storage failure")

        class InaccessibleStore(FakeStore):
            async def async_load(self):
                events.append("load")
                raise storage_error

        store = InaccessibleStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)

        class UnexpectedSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("binding must not start before storage is accessible")

        monkeypatch.setattr(activation, "TuyaBLELockSession", UnexpectedSession)

        with pytest.raises(Exception) as exc_info:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )

        assert type(exc_info.value).__name__ == "StorageUnavailableActivationError"
        assert exc_info.value.__cause__ is storage_error
        assert "sensitive" not in str(exc_info.value)
        assert "ble" not in events
        assert store.devices == {}

    asyncio.run(run_test())


def test_activation_retries_transient_persistence_failure_without_rebinding(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        seed = _seed()
        placeholder_store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, placeholder_store, seed, events)
        backend: dict[str, dict] = {}
        save_attempts = 0
        pair_attempts = 0

        class RetryStore:
            async def async_load(self):
                events.append("load")

            async def async_add_device(self, address, record):
                nonlocal save_attempts
                save_attempts += 1
                events.append(f"persist-{save_attempts}")
                if save_attempts == 1:
                    raise OSError("transient save failure")
                backend[address] = record

        class CountingSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_attempts
                pair_attempts += 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: RetryStore())
        monkeypatch.setattr(activation, "TuyaBLELockSession", CountingSession)

        record = await activation.async_activate_lock(
            hass,
            _entry(),
            address="AA:BB:CC:DD:EE:FF",
            device_uuid="discovered-uuid",
        )

        assert pair_attempts == 1
        assert save_attempts == 2
        assert backend == {"AA:BB:CC:DD:EE:FF": record}

    asyncio.run(run_test())


def test_activation_cancellation_waits_for_post_bind_persistence(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        placeholder_store = FakeStore(events)
        _install_common_fakes(
            monkeypatch, hass, placeholder_store, _seed(), events
        )
        backend: dict[str, dict] = {}
        persistence_started = asyncio.Event()
        finish_persistence = asyncio.Event()
        pair_attempts = 0
        disconnects = 0

        class BlockingStore:
            async def async_load(self):
                pass

            async def async_add_device(self, address, record):
                persistence_started.set()
                await finish_persistence.wait()
                backend[address] = record

        class CountingSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_attempts
                pair_attempts += 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

            async def async_disconnect(self):
                nonlocal disconnects
                disconnects += 1

        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: BlockingStore())
        monkeypatch.setattr(activation, "TuyaBLELockSession", CountingSession)

        task = asyncio.create_task(
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        )
        await persistence_started.wait()
        task.cancel("post-bind cancellation")
        await asyncio.sleep(0)
        cancellation_returned_before_save = task.done()
        finish_persistence.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task

        assert exc_info.value.args == ("post-bind cancellation",)
        assert cancellation_returned_before_save is False
        assert pair_attempts == 1
        assert disconnects == 1
        assert set(backend) == {"AA:BB:CC:DD:EE:FF"}
        assert hass.config_entries.reloads == ["hub-entry"]

    asyncio.run(run_test())


def test_activation_cancellation_during_disconnect_completes_post_bind_work(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        placeholder_store = FakeStore(events)
        _install_common_fakes(
            monkeypatch, hass, placeholder_store, _seed(), events
        )
        backend: dict[str, dict] = {}
        disconnect_started = asyncio.Event()
        finish_disconnect = asyncio.Event()
        disconnect_completed = False
        pair_attempts = 0

        class SavingStore:
            async def async_load(self):
                pass

            async def async_add_device(self, address, record):
                backend[address] = record

        class BlockingDisconnectSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_attempts
                pair_attempts += 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

            async def async_disconnect(self):
                nonlocal disconnect_completed
                disconnect_started.set()
                await finish_disconnect.wait()
                disconnect_completed = True

        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: SavingStore())
        monkeypatch.setattr(
            activation, "TuyaBLELockSession", BlockingDisconnectSession
        )

        task = asyncio.create_task(
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        )
        await disconnect_started.wait()
        task.cancel("disconnect cancellation")
        await asyncio.sleep(0)
        cancellation_returned_before_cleanup = task.done()
        finish_disconnect.set()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task

        assert exc_info.value.args == ("disconnect cancellation",)
        assert cancellation_returned_before_cleanup is False
        assert disconnect_completed is True
        assert pair_attempts == 1
        assert set(backend) == {"AA:BB:CC:DD:EE:FF"}
        assert hass.config_entries.reloads == ["hub-entry"]

    asyncio.run(run_test())


def test_activation_cancellation_wins_over_persistence_failure(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        placeholder_store = FakeStore(events)
        _install_common_fakes(
            monkeypatch, hass, placeholder_store, _seed(), events
        )
        persistence_started = asyncio.Event()
        finish_persistence = asyncio.Event()
        save_attempts = 0
        pair_attempts = 0
        disconnects = 0
        logged_errors: list[str] = []
        unhandled_contexts: list[dict] = []

        class FailingStore:
            async def async_load(self):
                pass

            async def async_add_device(self, _address, _record):
                nonlocal save_attempts
                save_attempts += 1
                persistence_started.set()
                await finish_persistence.wait()
                raise OSError("save failed")

        class CountingSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_attempts
                pair_attempts += 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

            async def async_disconnect(self):
                nonlocal disconnects
                disconnects += 1

        def fake_log_error(message, *args, **_kwargs):
            logged_errors.append(message % args)

        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: FailingStore())
        monkeypatch.setattr(activation, "TuyaBLELockSession", CountingSession)
        monkeypatch.setattr(activation._LOGGER, "error", fake_log_error)

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: unhandled_contexts.append(context)
        )
        try:
            task = asyncio.create_task(
                activation.async_activate_lock(
                    hass,
                    _entry(),
                    address="AA:BB:CC:DD:EE:FF",
                    device_uuid="discovered-uuid",
                )
            )
            await persistence_started.wait()
            task.cancel("persistence cancellation")
            finish_persistence.set()

            with pytest.raises(asyncio.CancelledError) as exc_info:
                await task
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert exc_info.value.args == ("persistence cancellation",)
        assert pair_attempts == 1
        assert disconnects == 1
        assert save_attempts == 3
        assert hass.config_entries.reloads == []
        assert logged_errors == [
            "Post-bind persistence failed for AA:BB:CC:DD:EE:FF during cancellation"
        ]
        assert unhandled_contexts == []

    asyncio.run(run_test())


def test_activation_reports_persistence_failure_after_three_attempts(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        placeholder_store = FakeStore(events)
        _install_common_fakes(
            monkeypatch, hass, placeholder_store, _seed(), events
        )
        save_attempts = 0
        pair_attempts = 0
        save_error = OSError("sensitive save failure")

        class FailingStore:
            async def async_load(self):
                pass

            async def async_add_device(self, _address, _record):
                nonlocal save_attempts
                save_attempts += 1
                raise save_error

        class CountingSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_attempts
                pair_attempts += 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: FailingStore())
        monkeypatch.setattr(activation, "TuyaBLELockSession", CountingSession)

        with pytest.raises(Exception) as exc_info:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )

        assert type(exc_info.value).__name__ == "PostBindPersistenceActivationError"
        assert exc_info.value.__cause__ is save_error
        assert "sensitive" not in str(exc_info.value)
        assert pair_attempts == 1
        assert save_attempts == 3
        assert hass.config_entries.reloads == []

    asyncio.run(run_test())


def test_activation_maps_post_bind_reload_failure(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)
        reload_error = RuntimeError("sensitive reload failure")

        class SuccessfulSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        async def failing_reload(_entry_id):
            events.append("reload")
            raise reload_error

        monkeypatch.setattr(activation, "TuyaBLELockSession", SuccessfulSession)
        monkeypatch.setattr(hass.config_entries, "async_reload", failing_reload)

        with pytest.raises(Exception) as exc_info:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )

        assert type(exc_info.value).__name__ == "PostBindPersistenceActivationError"
        assert exc_info.value.__cause__ is reload_error
        assert "sensitive" not in str(exc_info.value)
        assert set(store.devices) == {"AA:BB:CC:DD:EE:FF"}
        assert events.count("reload") == 1

    asyncio.run(run_test())


def test_activation_preserves_distinct_addresses_during_concurrent_saves(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        backend: dict[str, dict] = {}
        pair_count = 0
        both_pairing = asyncio.Event()

        async def fake_fetch(
            _hass,
            _device_uuid,
            _email,
            _password,
            _country,
            _region,
            *,
            device_mac,
        ):
            suffix = device_mac[-2:]
            return _seed(
                device_id=f"device-{suffix}",
                uuid=f"uuid-{suffix}",
                name=f"Lock {suffix}",
            )

        def fake_ble_lookup(_hass, address, *, connectable):
            assert connectable is True
            return types.SimpleNamespace(address=address)

        class SnapshotStore:
            def __init__(self):
                self.snapshot: dict[str, dict] = {}

            async def async_load(self):
                self.snapshot = dict(backend)
                await asyncio.sleep(0)

            async def async_add_device(self, address, record):
                self.snapshot[address] = record
                await asyncio.sleep(0)
                backend.clear()
                backend.update(self.snapshot)

        class ConcurrentSession(DisconnectableSession):
            def __init__(
                self,
                _hass,
                _ble_device,
                login_key,
                virtual_id,
                _device_uuid,
                **_kwargs,
            ):
                self.login_key = login_key
                self.virtual_id = virtual_id

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal pair_count
                pair_count += 1
                if pair_count == 2:
                    both_pairing.set()
                await both_pairing.wait()
                return self.login_key, self.virtual_id

        monkeypatch.setattr(activation, "async_fetch_auth_key", fake_fetch)
        monkeypatch.setattr(
            activation.bluetooth,
            "async_ble_device_from_address",
            fake_ble_lookup,
        )
        monkeypatch.setattr(activation, "DeviceStore", lambda _hass: SnapshotStore())
        monkeypatch.setattr(activation, "TuyaBLELockSession", ConcurrentSession)

        await asyncio.gather(
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:01",
            ),
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:02",
            ),
        )

        assert set(backend) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}

    asyncio.run(run_test())


def test_activation_rejects_incomplete_v5_seed(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(
            monkeypatch, hass, store, _seed(verify_key=""), events
        )

        class UnexpectedSession:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("session must not be created for an incomplete seed")

        monkeypatch.setattr(activation, "TuyaBLELockSession", UnexpectedSession)

        try:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        except activation.MissingActivationSeedError as exc:
            assert "verify_key" in str(exc)
        else:
            raise AssertionError("incomplete V5 seed was accepted")

        assert store.devices == {}
        assert "ble" not in events

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("updates", "requested_uuid", "expected_message"),
    [
        ({"encryptedAuthKey": ["00"] * 16}, "discovered-uuid", "auth_key must be a string"),
        ({"encryptedAuthKey": "00" * 15}, "discovered-uuid", "auth_key must be 16-byte hex"),
        ({"random": ["11"] * 16}, "discovered-uuid", "auth_random must be a string"),
        ({"random": "11" * 15}, "discovered-uuid", "auth_random must be 16-byte hex"),
        ({"verify_key": ["aa"] * 4}, "discovered-uuid", "verify_key must be a string"),
        ({"verify_key": "aa" * 3}, "discovered-uuid", "verify_key must be 4-byte hex"),
        ({"local_key": ["a"] * 16}, "discovered-uuid", "local_key must be an ASCII string"),
        ({"local_key": "a" * 15}, "discovered-uuid", "local_key must encode as exactly 16 ASCII bytes"),
        ({"local_key": "é" * 16}, "discovered-uuid", "local_key must encode as exactly 16 ASCII bytes"),
        ({"sec_key": ["b"] * 16}, "discovered-uuid", "sec_key must be an ASCII string"),
        ({"sec_key": "b" * 15}, "discovered-uuid", "sec_key must encode as exactly 16 ASCII bytes"),
        ({"sec_key": "é" * 16}, "discovered-uuid", "sec_key must encode as exactly 16 ASCII bytes"),
        ({"device_id": 123}, "discovered-uuid", "device_id must be a non-empty ASCII string"),
        ({"device_id": "dévice-id"}, "discovered-uuid", "device_id must be a non-empty ASCII string"),
        ({"device_id": ""}, "discovered-uuid", "device_id"),
        ({"uuid": 123}, "discovered-uuid", "uuid must be a non-empty ASCII string"),
        ({"uuid": "uuïd"}, "discovered-uuid", "uuid must be a non-empty ASCII string"),
        ({"uuid": ""}, "", "uuid"),
    ],
)
def test_activation_rejects_malformed_v5_seed(
    monkeypatch, updates, requested_uuid, expected_message
):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(
            monkeypatch,
            hass,
            store,
            _seed(**updates),
            events,
            requested_uuid=requested_uuid,
        )

        class UnexpectedSession:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("session must not be created for a malformed seed")

        monkeypatch.setattr(activation, "TuyaBLELockSession", UnexpectedSession)

        with pytest.raises(activation.MissingActivationSeedError) as exc_info:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid=requested_uuid,
            )

        assert expected_message in str(exc_info.value)
        assert store.devices == {}
        assert "ble" not in events

    asyncio.run(run_test())


def test_activation_rejects_unavailable_ble_device(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)
        monkeypatch.setattr(
            activation.bluetooth,
            "async_ble_device_from_address",
            lambda *_args, **_kwargs: None,
        )

        try:
            await activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            )
        except activation.BluetoothUnavailableError as exc:
            assert "AA:BB:CC:DD:EE:FF" in str(exc)
        else:
            raise AssertionError("unavailable BLE device was accepted")

        assert store.devices == {}

    asyncio.run(run_test())


def test_activation_serializes_attempts_per_address(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)
        active = 0
        max_active = 0

        class SlowSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key_hex):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0)
                active -= 1
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        monkeypatch.setattr(activation, "TuyaBLELockSession", SlowSession)

        await asyncio.gather(
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            ),
            activation.async_activate_lock(
                hass,
                _entry(),
                address="AA:BB:CC:DD:EE:FF",
                device_uuid="discovered-uuid",
            ),
        )

        assert max_active == 1

    asyncio.run(run_test())


def test_bluetooth_discovery_sets_formatted_unique_id_and_deduplicates(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        entry = _entry()
        in_progress_unique_ids = set()
        first_flow = _new_config_flow(
            monkeypatch,
            hass,
            entry,
            store,
            in_progress_unique_ids=in_progress_unique_ids,
        )
        second_flow = _new_config_flow(
            monkeypatch,
            hass,
            entry,
            store,
            in_progress_unique_ids=in_progress_unique_ids,
        )
        discovery = _discovery(
            "TyOS", address="AA-BB-CC-DD-EE-FF"
        )

        first_result = await _discover_and_check(first_flow, discovery)

        assert first_result["step_id"] == "confirm_new_device"
        assert first_flow.unique_id_calls == ["aa:bb:cc:dd:ee:ff"]
        assert first_flow.abort_if_configured_calls == 1
        with pytest.raises(_FakeAbortFlow, match="already_in_progress"):
            await _discover_and_check(second_flow, discovery)
        assert second_flow.unique_id_calls == ["aa:bb:cc:dd:ee:ff"]

    asyncio.run(run_test())


def test_local_setup_persists_validated_credentials_without_cloud_account(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        flow.current_entries = []

        async def profile_choices(_hass):
            return {"ba2qk177": "K3 BLE PRO 2 (ba2qk177)"}

        monkeypatch.setattr(config_flow, "async_get_profile_choices", profile_choices)

        menu = await flow.async_step_user()
        assert menu == {
            "type": "menu",
            "step_id": "user",
            "menu_options": ["cloud", "local"],
        }

        result = await flow.async_step_local(
            {
                "name": "Front Door",
                "device_mac": "aa-bb-cc-dd-ee-ff",
                "device_uuid": "uuidc064f275f947",
                "device_id": "device-id",
                "product_id": "ba2qk177",
                "local_key": "abcdefghijklmnop",
                "sec_key": "ponmlkjihgfedcba",
                "verify_key": "AABBCCDD",
                "auth_key": "AA" * 16,
                "auth_random": "11" * 16,
                "check_code": "12345678",
            }
        )

        assert result == {
            "type": "create_entry",
            "title": "Tuya BLE Access",
            "data": {"setup_method": "local"},
        }
        assert flow.unique_id_calls == ["tuya_ble_access_local"]
        assert store.devices == {
            "AA:BB:CC:DD:EE:FF": {
                "uuid": "uuidc064f275f947",
                "login_key": b"abcdef".hex(),
                "virtual_id": (b"device-id" + b"\x00" * 22)[:22].hex(),
                "auth_key": "aa" * 16,
                "auth_random": "11" * 16,
                "product_id": "ba2qk177",
                "name": "Front Door",
                "local_key": "abcdefghijklmnop",
                "sec_key": "ponmlkjihgfedcba",
                "verify_key": "aabbccdd",
                "check_code": "12345678",
                "cloud_dps": {},
            }
        }
        assert events == ["load", "persist"]

    asyncio.run(run_test())


def test_local_setup_rejects_invalid_credentials_before_persistence(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        flow.current_entries = []

        async def profile_choices(_hass):
            return {"ba2qk177": "K3 BLE PRO 2 (ba2qk177)"}

        monkeypatch.setattr(config_flow, "async_get_profile_choices", profile_choices)
        result = await flow.async_step_local(
            {
                "name": "Front Door",
                "device_mac": "not-a-mac",
                "device_uuid": "uuidc064f275f947",
                "device_id": "device-id",
                "product_id": "ba2qk177",
                "local_key": "too-short",
                "sec_key": "ponmlkjihgfedcba",
                "verify_key": "aabbccdd",
                "auth_key": "aa" * 16,
                "auth_random": "11" * 16,
                "check_code": "12345678",
            }
        )

        assert result["type"] == "form"
        assert result["step_id"] == "local"
        assert result["errors"] == {"base": "invalid_local_credentials"}
        assert store.devices == {}
        assert events == []

    asyncio.run(run_test())


def test_first_tyos_discovery_creates_only_hub_then_routes_to_confirmation(
    monkeypatch,
):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        flow.current_entries = []
        cloud_calls = 0

        async def fake_cloud_fetch(
            passed_hass,
            device_uuid,
            email,
            password,
            country,
            region,
            *,
            device_mac,
        ):
            nonlocal cloud_calls
            cloud_calls += 1
            assert passed_hass is hass
            assert device_uuid == ""
            assert (email, password, country, region) == (
                "user@example.com",
                "secret",
                "31",
                "eu",
            )
            assert device_mac == "AA:BB:CC:DD:EE:FF"
            return _seed(name="Factory-reset Lock")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fake_cloud_fetch)

        discovery_result = await _discover_and_check(flow, _discovery("TyOS"))
        assert discovery_result["step_id"] == "select_country"
        country_result = await flow.async_step_select_country({"country": "nl"})
        assert country_result["step_id"] == "cloud_login"
        create_result = await flow.async_step_cloud_login(
            {"email": "user@example.com", "password": "secret"}
        )

        assert create_result["step_id"] == "saved_locks"
        create_result = await flow.async_step_saved_locks({"device_mac": "AA:BB:CC:DD:EE:FF"})
        assert create_result["type"] == "create_entry"
        assert store.devices == {}
        assert "persist" not in events

        hub_entry = types.SimpleNamespace(
            entry_id="new-hub-entry", data=create_result["data"]
        )
        next_flow = _new_config_flow(monkeypatch, hass, hub_entry, store)
        next_result = await _discover_and_check(next_flow, _discovery("tYoS"))

        assert next_result["type"] == "form"
        assert next_result["step_id"] == "confirm_local_activation"
        assert cloud_calls == 1
        assert store.devices == {}

    asyncio.run(run_test())


def test_first_non_tyos_discovery_keeps_bound_device_persistence(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        flow.current_entries = []

        async def fake_cloud_fetch(*_args, **_kwargs):
            return _seed(name="Bound First Lock")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fake_cloud_fetch)

        await _discover_and_check(flow, _discovery("Bound First Lock"))
        await flow.async_step_select_country({"country": "nl"})
        result = await flow.async_step_cloud_login(
            {"email": "user@example.com", "password": "secret"}
        )

        assert result["step_id"] == "saved_locks"
        result = await flow.async_step_saved_locks({"device_mac": "AA:BB:CC:DD:EE:FF"})
        assert result["type"] == "create_entry"
        assert store.devices["AA:BB:CC:DD:EE:FF"]["name"] == "Bound First Lock"
        assert events.count("persist") == 1

    asyncio.run(run_test())


def test_tyos_discovery_routes_to_local_activation_confirmation(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)

        async def unexpected_cloud_fetch(*_args, **_kwargs):
            return _seed()

        async def unexpected_activation(*_args, **_kwargs):
            raise AssertionError("showing the confirmation must not activate the lock")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected_cloud_fetch)
        monkeypatch.setattr(
            config_flow, "async_activate_lock", unexpected_activation, raising=False
        )

        result = await _discover_and_check(flow, _discovery("tYoS"))

        assert result["type"] == "form"
        assert result["step_id"] == "confirm_new_device"
        assert result["errors"] == {}
        assert result["description_placeholders"] == {
            "name": "tYoS",
            "mac": "AA:BB:CC:DD:EE:FF",
        }

    asyncio.run(run_test())


def test_activation_confirmation_calls_shared_orchestrator(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        entry = _entry()
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, entry, store)
        calls = []

        async def unexpected_cloud_fetch(*_args, **_kwargs):
            return _seed()

        async def fake_activate(passed_hass, passed_entry, **kwargs):
            calls.append((passed_hass, passed_entry, kwargs))
            return {"name": "Activated Lock"}

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected_cloud_fetch)
        monkeypatch.setattr(
            config_flow, "async_activate_lock", fake_activate, raising=False
        )
        monkeypatch.setattr(
            config_flow,
            "_decrypt_uuid",
            lambda service_data, encrypted_id: (
                "discovered-uuid"
                if service_data == b"service-data" and encrypted_id == bytes(range(4, 20))
                else "unexpected-uuid"
            ),
        )

        await _discover_and_check(flow,
            _discovery("TyOS", with_encrypted_uuid=True)
        )
        refreshed_entry = _entry(password="refreshed-secret")
        flow.current_entries = [refreshed_entry]
        checked = await flow.async_step_confirm_new_device({})
        assert checked["step_id"] == "confirm_local_activation"
        assert calls == []
        result = await flow.async_step_confirm_new_device({})

        assert calls == [
            (
                hass,
                refreshed_entry,
                {
                    "address": "AA:BB:CC:DD:EE:FF",
                    "device_uuid": "discovered-uuid",
                    "name": "TyOS",
                    "activation_seed": activation.validate_activation_seed(_seed()),
                },
            )
        ]
        assert result == {"type": "abort", "reason": "device_added"}
        assert hass.config_entries.reloads == []
        assert store.devices == {}

    asyncio.run(run_test())


@pytest.mark.parametrize(
    "current_entries",
    [[], [_entry(entry_id="replacement-hub")]],
    ids=["removed", "replaced"],
)
def test_activation_confirmation_rejects_changed_hub_entry(
    monkeypatch, current_entries
):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        activation_calls = 0

        async def fake_activate(*_args, **_kwargs):
            nonlocal activation_calls
            activation_calls += 1
            return {}

        monkeypatch.setattr(
            config_flow, "async_activate_lock", fake_activate, raising=False
        )

        await _discover_and_check(flow, _discovery("TyOS"))
        flow.current_entries = current_entries
        result = await flow.async_step_confirm_new_device({})

        assert result["type"] == "form"
        assert result["errors"] == {"base": "integration_changed"}
        assert activation_calls == 0

    asyncio.run(run_test())


def test_activation_confirmation_detects_concurrent_device_add(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        activation_calls = 0

        async def fake_activate(*_args, **_kwargs):
            nonlocal activation_calls
            activation_calls += 1
            return {}

        monkeypatch.setattr(
            config_flow, "async_activate_lock", fake_activate, raising=False
        )

        await _discover_and_check(flow, _discovery("TyOS"))
        store.devices["AA:BB:CC:DD:EE:FF"] = {"name": "Concurrent Lock"}
        result = await flow.async_step_confirm_new_device({})

        assert result == {"type": "abort", "reason": "already_configured"}
        assert activation_calls == 0
        assert events.count("load") == 3

    asyncio.run(run_test())


def test_activation_confirmation_maps_store_reload_failure(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)

        class FailingReloadStore(FakeStore):
            async def async_load(self):
                self.events.append("load")
                if self.events.count("load") == 3:
                    raise OSError("sensitive form storage failure")

        store = FailingReloadStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)
        activation_calls = 0

        async def fake_activate(*_args, **_kwargs):
            nonlocal activation_calls
            activation_calls += 1
            return {}

        monkeypatch.setattr(
            config_flow, "async_activate_lock", fake_activate, raising=False
        )

        await _discover_and_check(flow, _discovery("TyOS"))
        result = await flow.async_step_confirm_new_device({})

        assert result["type"] == "form"
        assert result["errors"] == {"base": "activation_storage_unavailable"}
        assert "sensitive" not in str(result)
        assert activation_calls == 0

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("error", "expected_base"),
    [
        (
            config_flow_activation.MissingActivationSeedError(
                "seed contained secret-value"
            ),
            "activation_seed_missing",
        ),
        (
            config_flow_activation.BluetoothUnavailableError(
                "adapter contained secret-value"
            ),
            "bluetooth_unavailable",
        ),
        (
            config_flow_activation.DeviceAlreadyBoundActivationError(
                "bound contained secret-value"
            ),
            "device_already_bound",
        ),
        (
            config_flow_activation.PairingFailedActivationError(
                "PAIR contained secret-value"
            ),
            "pairing_failed",
        ),
        (
            config_flow_activation.BindVerificationActivationError(
                "bind contained secret-value"
            ),
            "bind_verification_failed",
        ),
        (
            config_flow_activation.CloudFetchActivationError(
                "cloud contained secret-value"
            ),
            "activation_cloud_fetch_failed",
        ),
        (
            config_flow_activation.StorageUnavailableActivationError(
                "storage contained secret-value"
            ),
            "activation_storage_unavailable",
        ),
        (
            config_flow_activation.PostBindPersistenceActivationError(
                "persistence contained secret-value"
            ),
            "activation_credentials_not_saved",
        ),
        (RuntimeError("activation contained secret-value"), "activation_failed"),
    ],
)
def test_activation_confirmation_maps_errors_without_leaking_details(
    monkeypatch, error, expected_base
):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)

        async def unexpected_cloud_fetch(*_args, **_kwargs):
            return _seed()

        async def failing_activation(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected_cloud_fetch)
        monkeypatch.setattr(
            config_flow, "async_activate_lock", failing_activation, raising=False
        )

        await _discover_and_check(flow, _discovery("TYOS"))
        result = await flow.async_step_confirm_new_device({})

        assert result["type"] == "form"
        assert result["step_id"] == "confirm_new_device"
        assert result["errors"] == {"base": expected_base}
        assert "secret-value" not in str(result)

    asyncio.run(run_test())


def test_activation_outcome_error_copy_matches_translations():
    expected_keys = {
        "activation_seed_missing",
        "bluetooth_unavailable",
        "device_already_bound",
        "pairing_failed",
        "bind_verification_failed",
        "activation_cloud_fetch_failed",
        "activation_storage_unavailable",
        "activation_credentials_not_saved",
        "activation_failed",
        "integration_changed",
    }
    strings = json.loads((ROOT / "strings.json").read_text())
    translations = json.loads((ROOT / "translations" / "en.json").read_text())

    assert expected_keys <= strings["config"]["error"].keys()
    assert expected_keys <= translations["config"]["error"].keys()
    for key in expected_keys:
        assert strings["config"]["error"][key] == translations["config"]["error"][key]

    assert "factory-reset" not in strings["config"]["error"][
        "activation_failed"
    ].lower()
    persistence_copy = strings["config"]["error"][
        "activation_credentials_not_saved"
    ].lower()
    assert "could not be saved" in persistence_copy
    assert "reconfigure" in persistence_copy


def test_non_tyos_bound_lock_keeps_auto_add_behavior(monkeypatch):
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)

        async def fake_cloud_fetch(
            passed_hass,
            device_uuid,
            email,
            password,
            country,
            region,
            *,
            device_mac,
        ):
            assert passed_hass is hass
            assert device_uuid == ""
            assert (email, password, country, region) == (
                "user@example.com",
                "secret",
                "31",
                "eu",
            )
            assert device_mac == "AA:BB:CC:DD:EE:FF"
            return _seed(name="Bound Lock")

        async def unexpected_activation(*_args, **_kwargs):
            raise AssertionError("bound locks must retain cloud auto-add")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fake_cloud_fetch)
        monkeypatch.setattr(
            config_flow, "async_activate_lock", unexpected_activation, raising=False
        )

        result = await _discover_and_check(flow, _discovery("Bound Lock"))

        assert result == {"type": "abort", "reason": "device_added"}
        assert store.devices["AA:BB:CC:DD:EE:FF"]["name"] == "Bound Lock"
        assert hass.config_entries.reloads == ["hub-entry"]

    asyncio.run(run_test())


def test_discovered_non_lock_is_not_auto_added(monkeypatch):
    """A SigMesh gateway advertises the same FD50 service UUID as the lock, so
    auto-add must reject anything whose cloud category is not a lock."""
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)

        async def gateway_cloud_fetch(*_args, **_kwargs):
            seed = _seed(name="SigMesh Gateway")
            seed["category"] = "wg2"  # gateway, not a lock
            return seed

        monkeypatch.setattr(
            config_flow, "async_fetch_auth_key", gateway_cloud_fetch
        )

        result = await _discover_and_check(flow, _discovery("SigMesh Gateway"))

        assert result == {"type": "abort", "reason": "no_saved_locks"}
        assert store.devices == {}
        assert hass.config_entries.reloads == []

    asyncio.run(run_test())


def test_discovered_lock_category_is_still_auto_added(monkeypatch):
    """A real lock category must keep the existing auto-add behaviour."""
    async def run_test():
        events: list[str] = []
        hass = FakeHass(events)
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, hass, _entry(), store)

        async def lock_cloud_fetch(*_args, **_kwargs):
            seed = _seed(name="Bound Lock")
            seed["category"] = "jtmspro"
            return seed

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", lock_cloud_fetch)

        result = await _discover_and_check(flow, _discovery("Bound Lock"))

        assert result == {"type": "abort", "reason": "device_added"}
        assert store.devices["AA:BB:CC:DD:EE:FF"]["name"] == "Bound Lock"

    asyncio.run(run_test())


def test_activate_service_registers_schema_and_response_once():
    async def run_test():
        hass = FakeServiceHass([_entry()])

        registration = await _activate_service_registration(hass)
        await services.async_register_services(hass)

        _, _, _, schema, supports_response = registration
        assert supports_response == ServiceSupportsResponse.OPTIONAL
        assert hass.admin_service_registrations[0] == registration
        assert hass.admin_service_registrations[1][0:2] == (
            "tuya_ble_access",
            "register_credential",
        )
        assert ("tuya_ble_access", "activate") not in (
            hass.services.plain_registrations
        )
        assert ("tuya_ble_access", "register_credential") not in (
            hass.services.plain_registrations
        )
        assert schema(
            {
                "address": "aa-bb-cc-dd-ee-ff",
                "device_uuid": "device-uuid",
                "name": "Back Door",
            }
        ) == {
            "address": "AA:BB:CC:DD:EE:FF",
            "device_uuid": "device-uuid",
            "name": "Back Door",
        }
        assert schema(
            {"address": "cba20d00-224d-11e6-9fb8-0002a5d5c51b"}
        ) == {"address": "CBA20D00-224D-11E6-9FB8-0002A5D5C51B"}
        with pytest.raises(_FakeInvalid, match="required key"):
            schema({})
        with pytest.raises(_FakeInvalid, match="must not be empty"):
            schema({"address": "   "})
        with pytest.raises(_FakeInvalid, match="expected str"):
            schema({"address": 123})

        activate_registrations = [
            item
            for item in hass.services.registrations
            if item[0:2] == ("tuya_ble_access", "activate")
        ]
        assert len(activate_registrations) == 1
        assert len(hass.services.registrations) == 13

    asyncio.run(run_test())


def test_report_factory_reset_is_scoped_admin_action_without_ble():
    async def run_test():
        mac = "AA:BB:CC:DD:EE:FF"
        other_mac = "11:22:33:44:55:66"
        refreshed = []
        entry = _entry()
        # No BLE session or cloud API exists on these offline coordinators.
        entry.runtime_data = types.SimpleNamespace(coordinators={
            address: types.SimpleNamespace(
                async_update_listeners=lambda address=address: refreshed.append(address),
                last_update_success=False,
            )
            for address in (mac, other_mac)
        })
        hass = FakeServiceHass([entry])
        reset_calls = []

        async def report_reset(address):
            reset_calls.append(address)
            return {"credentials": 3, "temp_passwords": 1}

        hass.data = {"tuya_ble_access": {"credential_store": types.SimpleNamespace(
            async_report_factory_reset=report_reset,
        )}}
        await services.async_register_services(hass)
        registration = hass.services.registration("tuya_ble_access", "report_factory_reset")
        assert registration in hass.admin_service_registrations
        _, _, handler, schema, supports_response = registration
        assert supports_response == ServiceSupportsResponse.OPTIONAL
        with pytest.raises(_FakeInvalid, match="required key"):
            schema({})
        result = await handler(types.SimpleNamespace(data=schema({"device_id": mac})))
        assert result == {"mac": mac, "removed": {"credentials": 3, "temp_passwords": 1}}
        assert reset_calls == [mac]
        assert refreshed == [mac]

        async def failed_reset(address):
            raise OSError("disk unavailable")

        hass.data["tuya_ble_access"]["credential_store"].async_report_factory_reset = failed_reset
        with pytest.raises(OSError):
            await handler(types.SimpleNamespace(data={"device_id": mac}))
        assert refreshed == [mac]

    asyncio.run(run_test())


def test_register_credential_associates_existing_slot_without_ble():
    async def run_test():
        mac = "AA:BB:CC:DD:EE:FF"
        entry = _entry()
        entry.runtime_data = types.SimpleNamespace(coordinators={mac: object()})
        hass = FakeServiceHass([entry])
        hass.states = types.SimpleNamespace(
            get=lambda entity_id: (
                types.SimpleNamespace(name="Tuya Lock Test")
                if entity_id == "person.tuya_lock_test"
                else None
            )
        )

        class FakeCredentialStore:
            def __init__(self):
                self.member = None
                self.credential = None

            def find_credential(self, lock_entry_id, cred_type, hw_id):
                if self.credential and (
                    self.credential.lock_entry_id,
                    self.credential.cred_type,
                    self.credential.hw_id,
                ) == (lock_entry_id, cred_type, hw_id):
                    return self.credential
                return None

            def get_member_by_name(self, name):
                return self.member if self.member and self.member.name == name else None

            async def async_add_member(self, name, person_entity_id=None):
                self.member = types.SimpleNamespace(
                    member_id=1,
                    name=name,
                    person_entity_id=person_entity_id,
                )
                return self.member

            async def async_update_member(self, member_id, **kwargs):
                assert member_id == self.member.member_id
                for key, value in kwargs.items():
                    setattr(self.member, key, value)
                return self.member

            def get_member(self, member_id):
                return self.member if self.member.member_id == member_id else None

            async def async_add_credential(self, **kwargs):
                self.credential = types.SimpleNamespace(
                    credential_id="credential-1",
                    **kwargs,
                )
                return self.credential

        store = FakeCredentialStore()
        hass.data = {"tuya_ble_access": {"credential_store": store}}
        await services.async_register_services(hass)
        _, _, handler, schema, supports_response = hass.services.registration(
            "tuya_ble_access", "register_credential"
        )
        assert supports_response == ServiceSupportsResponse.OPTIONAL
        call_data = schema(
            {
                "device_id": mac,
                "person": "person.tuya_lock_test",
                "cred_type": "fingerprint",
                "hw_id": 2,
                "name": "Right index",
            }
        )

        result = await handler(types.SimpleNamespace(data=call_data))

        assert result == {
            "credential_id": "credential-1",
            "member_id": 1,
            "member_name": "Tuya Lock Test",
            "credential_type": "fingerprint",
            "hardware_id": 2,
            "name": "Right index",
            "replaced": None,
        }
        assert store.credential.lock_entry_id == mac
        assert store.credential.cred_type == 2
        assert store.credential.hw_id == 2
        assert store.member.person_entity_id == "person.tuya_lock_test"

        # A hardware slot holds one credential, so re-registering it reassigns
        # rather than erroring — the caller is told what it replaced.
        again = schema(
            {
                "device_id": mac,
                "person": "person.tuya_lock_test",
                "cred_type": "fingerprint",
                "hw_id": 2,
                "name": "Left thumb",
            }
        )
        result2 = await handler(types.SimpleNamespace(data=again))
        assert result2["replaced"] == "Right index"
        assert result2["name"] == "Left thumb"
        assert store.credential.name == "Left thumb"

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        ("aabbccddeeff", "AA:BB:CC:DD:EE:FF"),
        (
            "cba20d00-224d-11e6-9fb8-0002a5d5c51b",
            "CBA20D00-224D-11E6-9FB8-0002A5D5C51B",
        ),
    ],
)
def test_activate_schema_normalizes_valid_bluetooth_address(address, expected):
    assert services.ACTIVATE_SCHEMA({"address": address}) == {
        "address": expected
    }


@pytest.mark.parametrize(
    "address",
    [
        "not-a-bluetooth-address",
        "AA:BB:CC:DD:EE",
        "AA:BB-CC:DD:EE:FF",
        "AABBCCDDEEFG",
        "CBA20D00-224D-11E6-9FB8-0002A5D5C51",
        "CBA20D00-224D-11E6-9FB8-0002A5D5C51G",
        "{CBA20D00-224D-11E6-9FB8-0002A5D5C51B}",
    ],
)
def test_activate_schema_rejects_malformed_bluetooth_address(address):
    with pytest.raises(
        _FakeInvalid, match="must be a MAC address or CoreBluetooth UUID"
    ):
        services.ACTIVATE_SCHEMA({"address": address})


def test_activate_service_metadata_documents_fields_and_selectors():
    yaml = pytest.importorskip("yaml")
    metadata = yaml.safe_load((ROOT / "services.yaml").read_text())

    activate = metadata["activate"]
    assert "factory-reset" in activate["description"]
    assert activate["fields"]["address"]["required"] is True
    assert "BLE identifier" in activate["fields"]["address"]["description"]
    assert activate["fields"]["address"]["selector"] == {"text": None}
    assert activate["fields"]["device_uuid"]["required"] is False
    assert activate["fields"]["device_uuid"]["selector"] == {"text": None}
    assert activate["fields"]["name"]["required"] is False
    assert activate["fields"]["name"]["selector"] == {"text": None}


def test_activate_service_rejects_missing_hub():
    async def run_test():
        hass = FakeServiceHass([])
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(
            ServiceHomeAssistantError,
            match="No active Tuya BLE Access hub is configured",
        ):
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

    asyncio.run(run_test())


def test_activate_service_rejects_ambiguous_hubs():
    async def run_test():
        hass = FakeServiceHass(
            [_entry(entry_id="hub-1"), _entry(entry_id="hub-2")]
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(
            ServiceHomeAssistantError,
            match="Multiple active Tuya BLE Access hubs are configured; activation is ambiguous",
        ):
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

    asyncio.run(run_test())


def test_activate_service_rejects_unloaded_hub(monkeypatch):
    async def run_test():
        hass = FakeServiceHass([_entry(state="not_loaded")])
        activation_calls = 0

        async def unexpected_activation(*_args, **_kwargs):
            nonlocal activation_calls
            activation_calls += 1

        monkeypatch.setattr(
            services, "async_activate_lock", unexpected_activation, raising=False
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(
            ServiceHomeAssistantError,
            match="The Tuya BLE Access hub is not loaded",
        ):
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

        assert activation_calls == 0

    asyncio.run(run_test())


def test_activate_service_ignores_disabled_and_ignored_hubs(monkeypatch):
    async def run_test():
        loaded_entry = _entry(entry_id="loaded-hub")
        hass = FakeServiceHass(
            [
                _entry(entry_id="disabled-hub", disabled_by="user"),
                _entry(entry_id="ignored-hub", source="ignore"),
                loaded_entry,
            ]
        )
        selected_entries = []

        async def fake_activate(_hass, entry, **_kwargs):
            selected_entries.append(entry)
            return {"name": "Cloud Lock", "uuid": "cloud-uuid"}

        monkeypatch.setattr(
            services, "async_activate_lock", fake_activate, raising=False
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        await handler(
            types.SimpleNamespace(
                data=schema({"address": "AA:BB:CC:DD:EE:FF"})
            )
        )

        assert selected_entries == [loaded_entry]

    asyncio.run(run_test())


def test_activate_service_calls_orchestrator_and_returns_record(monkeypatch):
    async def run_test():
        entry = _entry()
        hass = FakeServiceHass([entry])
        calls = []

        async def fake_activate(passed_hass, passed_entry, **kwargs):
            calls.append((passed_hass, passed_entry, kwargs))
            return {"name": "Cloud Lock", "uuid": "cloud-uuid"}

        monkeypatch.setattr(
            services, "async_activate_lock", fake_activate, raising=False
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)
        data = schema(
            {
                "address": "aa-bb-cc-dd-ee-ff",
                "device_uuid": "advertised-uuid",
                "name": "Requested Name",
            }
        )

        result = await handler(types.SimpleNamespace(data=data))

        assert calls == [
            (
                hass,
                entry,
                {
                    "address": "AA:BB:CC:DD:EE:FF",
                    "device_uuid": "advertised-uuid",
                    "name": "Requested Name",
                },
            )
        ]
        assert result == {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Cloud Lock",
            "uuid": "cloud-uuid",
        }
        assert hass.config_entry_service_calls == [("tuya_ble_access", None)]

    asyncio.run(run_test())


def test_activate_service_maps_activation_error(monkeypatch):
    async def run_test():
        hass = FakeServiceHass([_entry()])
        activation_error = services_activation.ActivationError(
            "Could not fetch lock activation data"
        )

        async def failing_activation(*_args, **_kwargs):
            raise activation_error

        monkeypatch.setattr(
            services, "async_activate_lock", failing_activation, raising=False
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(
            ServiceHomeAssistantError,
            match="Could not activate lock: Could not fetch lock activation data",
        ) as exc_info:
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

        assert exc_info.value.__cause__ is activation_error

    asyncio.run(run_test())


def test_activate_service_sanitizes_unexpected_error(monkeypatch):
    async def run_test():
        hass = FakeServiceHass([_entry()])
        unexpected_error = RuntimeError("fake-secret-value")
        logged_errors = []

        async def failing_activation(*_args, **_kwargs):
            raise unexpected_error

        def fake_log_error(message, *args, **kwargs):
            logged_errors.append((message % args, kwargs))

        monkeypatch.setattr(
            services, "async_activate_lock", failing_activation, raising=False
        )
        monkeypatch.setattr(services._LOGGER, "error", fake_log_error)
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(ServiceHomeAssistantError) as exc_info:
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

        assert str(exc_info.value) == "Could not activate lock due to an unexpected error"
        assert "fake-secret-value" not in str(exc_info.value)
        assert exc_info.value.__cause__ is unexpected_error
        assert logged_errors == [
            (
                "Unexpected error activating lock AA:BB:CC:DD:EE:FF",
                {"exc_info": True},
            )
        ]

    asyncio.run(run_test())


def test_activate_service_preserves_cancellation(monkeypatch):
    async def run_test():
        hass = FakeServiceHass([_entry()])

        async def cancelled_activation(*_args, **_kwargs):
            raise asyncio.CancelledError("service cancelled")

        monkeypatch.setattr(
            services, "async_activate_lock", cancelled_activation, raising=False
        )
        _, _, handler, schema, _ = await _activate_service_registration(hass)

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await handler(
                types.SimpleNamespace(
                    data=schema({"address": "AA:BB:CC:DD:EE:FF"})
                )
            )

        assert exc_info.value.args == ("service cancelled",)

    asyncio.run(run_test())


# ---- Reconfigure from a locally-set-up hub (post-repair key restore) --------

class _RecfgConfigEntries:
    """Minimal config_entries supporting get/update/reload for reconfigure."""

    def __init__(self, entry):
        self._entry = entry
        self.reloads: list[str] = []

    def async_get_entry(self, entry_id):
        return self._entry if entry_id == self._entry.entry_id else None

    def async_update_entry(self, entry, *, data):
        entry.data = data

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)


def _recfg_flow(entry, refresh_recorder):
    hass = types.SimpleNamespace(config_entries=_RecfgConfigEntries(entry))
    flow = config_flow.TuyaBLELockConfigFlow()
    flow.hass = hass
    flow.context = {"entry_id": entry.entry_id}
    # Route the function-local `from .tuya_cloud import async_refresh_all_devices`
    # to our recorder. The loader tears its stub down after import, so register
    # a fresh one for the call-time import to resolve.
    mod = sys.modules.get(f"{PACKAGE}.tuya_cloud")
    if mod is None:
        mod = types.ModuleType(f"{PACKAGE}.tuya_cloud")
        sys.modules[f"{PACKAGE}.tuya_cloud"] = mod
    mod.async_refresh_all_devices = refresh_recorder
    return flow, hass


def test_reconfigure_local_hub_shows_country_and_region():
    """A local hub (no stored cloud creds) must not be refused: the form must
    appear and ask for country + region so credentials can be supplied."""
    async def run_test():
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={"setup_method": "local"},
        )

        async def _never(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("refresh should not run before submit")

        flow, _ = _recfg_flow(entry, _never)
        result = await flow.async_step_reconfigure()

        assert result["type"] == "form"
        assert result["step_id"] == "reconfigure"
        assert "tuya_country_code" in result["data_schema"]
        assert "tuya_region" in result["data_schema"]

    asyncio.run(run_test())


def test_reconfigure_local_hub_submit_refreshes_and_upgrades_to_cloud():
    """Submitting the account details must fetch fresh keys, persist the
    credentials, mark the hub cloud-backed, and reload."""
    async def run_test():
        entry = types.SimpleNamespace(
            entry_id="e1",
            data={"setup_method": "local"},
        )
        calls: dict = {}

        async def fake_refresh(hass, passed_entry, *, new_password):
            # Credentials the helper relies on must already be on the entry.
            calls["email"] = passed_entry.data.get("tuya_email")
            calls["country"] = passed_entry.data.get("tuya_country_code")
            calls["region"] = passed_entry.data.get("tuya_region")
            calls["password"] = new_password
            return 1

        flow, hass = _recfg_flow(entry, fake_refresh)
        result = await flow.async_step_reconfigure({
            "email": "user@example.com",
            "password": "hunter2",
            "tuya_country_code": "31",
            "tuya_region": "eu",
        })

        assert result == {"type": "abort", "reason": "reauth_successful"}
        assert calls == {
            "email": "user@example.com",
            "country": "31",
            "region": "eu",
            "password": "hunter2",
        }
        # Upgraded to cloud and credentials persisted for the cloud button.
        assert entry.data["setup_method"] == "cloud"
        assert entry.data["tuya_password"] == "hunter2"
        assert entry.data["tuya_email"] == "user@example.com"
        assert hass.config_entries.reloads == ["e1"]

    asyncio.run(run_test())


@pytest.mark.parametrize("discovery", [False, True])
def test_domain_migration_requires_confirmation_and_preserves_config(monkeypatch, discovery):
    async def run():
        hass = FakeHass([])
        source = types.SimpleNamespace(entry_id="legacy", version=2, disabled_by=None,
            unique_id="existing-account", data={"setup_method": "local"}, options={"test": 1})
        hass.config_entries.async_entries = lambda domain: [source] if domain == "tuya_ble_lock" else []
        flow = _new_config_flow(monkeypatch, hass, source, FakeStore([]))
        flow.current_entries = []
        form = await _discover_and_check(flow, _discovery("TyOS")) if discovery else await flow.async_step_user()
        assert form["step_id"] == "migrate"
        assert flow.unique_id_calls == []
        result = await flow.async_step_migrate({})
        assert result["type"] == "create_entry"
        assert result["data"] == {"setup_method": "local", "legacy_domain_migration": {"entry_id": "legacy"}}
        assert result["options"] == source.options
        assert source.data == {"setup_method": "local"}
        assert flow.unique_id_calls == ["existing-account"]
    asyncio.run(run())


@pytest.mark.parametrize("version,disabled", [(1, None), (2, "user")])
def test_domain_migration_rejects_unsupported_source(monkeypatch, version, disabled):
    async def run():
        hass = FakeHass([])
        source = types.SimpleNamespace(version=version, disabled_by=disabled)
        hass.config_entries.async_entries = lambda domain: [source]
        flow = _new_config_flow(monkeypatch, hass, source, FakeStore([]))
        flow.current_entries = []
        assert await flow.async_step_user() == {"type": "abort", "reason": "legacy_migration_unavailable"}
    asyncio.run(run())


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"device_id": ""}, "pair_in_app"),
        ({"category": "gateway"}, "not_a_lock"),
        ({"verify_key": ""}, "activation_seed_missing"),
        ({"random": "invalid"}, "activation_seed_missing"),
        ({"local_key": "short"}, "activation_seed_missing"),
    ],
)
def test_tyos_checks_account_and_keys_before_offering_activation(monkeypatch, updates, reason):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)
        calls = []

        async def fetch(*_args, **_kwargs):
            calls.append("fetch")
            return _seed(**updates)

        async def activate(*_args, **_kwargs):
            raise AssertionError("Unverified discovery must never activate a lock")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fetch)
        monkeypatch.setattr(config_flow, "async_activate_lock", activate)
        result = await _discover_and_check(flow, _discovery("TyOS"))
        assert result["step_id"] == "check_device"
        assert result["errors"] == {"base": reason}
        assert calls == ["fetch"]
        assert flow._activation_seed is None
        assert store.devices == {}

    asyncio.run(run_test())


def test_tyos_cloud_failure_is_not_reported_as_missing_pairing(monkeypatch):
    async def run_test():
        events = []
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), FakeStore(events))

        async def fetch(*_args, **_kwargs):
            raise TimeoutError("private account details")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fetch)
        result = await _discover_and_check(flow, _discovery("TyOS"))
        assert result["step_id"] == "check_device"
        assert result["errors"] == {"base": "cloud_fetch_failed"}
        assert flow._activation_seed is None

    asyncio.run(run_test())


def test_activation_confirmation_reuses_checked_keys_without_logging_in_again(monkeypatch):
    async def run_test():
        events = []
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), FakeStore(events))
        calls = []

        async def fetch(*_args, **_kwargs):
            calls.append("fetch")
            return _seed()

        async def activate(*_args, **kwargs):
            calls.append("activate")
            assert kwargs["activation_seed"] == activation.validate_activation_seed(_seed())

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fetch)
        monkeypatch.setattr(config_flow, "async_activate_lock", activate)
        shown = await _discover_and_check(flow, _discovery("TyOS"))
        assert shown["step_id"] == "confirm_new_device"
        assert "abcdefghijklmnop" not in str(shown)
        assert calls == ["fetch"]
        # GET/polling the form must neither log in again nor activate.
        await flow.async_step_confirm_new_device()
        assert calls == ["fetch"]
        result = await flow.async_step_confirm_new_device({})
        assert result == {"type": "abort", "reason": "device_added"}
        assert calls == ["fetch", "activate"]

    asyncio.run(run_test())


def test_prechecked_activation_does_not_fetch_cloud_again(monkeypatch):
    async def run_test():
        events = []
        hass = FakeHass(events)
        store = FakeStore(events)
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)

        async def unexpected_fetch(*_args, **_kwargs):
            raise AssertionError("Checked credentials must not cause another cloud login")

        class Session(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_pair_first_activation(self, _auth_key):
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        monkeypatch.setattr(activation, "async_fetch_auth_key", unexpected_fetch)
        monkeypatch.setattr(activation, "TuyaBLELockSession", Session)
        await activation.async_activate_lock(
            hass, _entry(), address="AA:BB:CC:DD:EE:FF", activation_seed=_seed()
        )
        assert store.devices["AA:BB:CC:DD:EE:FF"]["local_key"] == "abcdefghijklmnop"
        assert "fetch" not in events

    asyncio.run(run_test())


@pytest.mark.parametrize("discovery_name", ["TyOS", "Bound Lock"])
def test_first_lock_missing_from_account_cannot_be_added(monkeypatch, discovery_name):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)
        flow.current_entries = []

        async def missing(*_args, **_kwargs):
            return {"uuid": "uuid-from-advertisement", "device_id": ""}

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", missing)
        await _discover_and_check(flow, _discovery(discovery_name))
        await flow.async_step_select_country({"country": "nl"})
        result = await flow.async_step_cloud_login({"email": "user@example.com", "password": "secret"})
        assert result["type"] == "create_entry"  # Account saved, no lock invented.
        assert store.devices == {}

    asyncio.run(run_test())


def test_failed_pairing_is_retried_with_persisted_keys_without_cloud(monkeypatch):
    async def run_test():
        events = []
        hass = FakeHass(events)
        store = FakeStore(events)
        entry = _entry()
        flow = _new_config_flow(monkeypatch, hass, entry, store)
        calls = []

        async def fetch(*_args, **_kwargs):
            calls.append("fetch")
            return _seed()

        async def failing_pair(*_args, **kwargs):
            calls.append("pair")
            assert kwargs["activation_seed"]["local_key"] == "abcdefghijklmnop"
            raise config_flow_activation.PairingFailedActivationError()

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fetch)
        monkeypatch.setattr(config_flow, "async_activate_lock", failing_pair)
        await _discover_and_check(flow, _discovery("TyOS"))
        assert store.devices == {}
        result = await flow.async_step_confirm_new_device({})
        assert result["errors"] == {"base": "pairing_failed"}
        assert store.activation_seeds["AA:BB:CC:DD:EE:FF"]

        local_entry = _entry()
        local_entry.data = {"setup_method": "local"}
        next_flow = _new_config_flow(monkeypatch, hass, local_entry, store)

        async def unexpected_cloud(*_args, **_kwargs):
            raise AssertionError("Persisted activation keys must work with no cloud account")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected_cloud)
        result = await _discover_and_check(next_flow, _discovery("TyOS"))
        assert result["step_id"] == "confirm_local_activation"
        assert result["errors"] == {}
        await next_flow.async_step_confirm_local_activation({})
        assert calls == ["fetch", "pair", "pair"]
        assert store.devices == {}

    asyncio.run(run_test())


def test_known_reset_lock_uses_existing_record_without_cloud(monkeypatch):
    async def run_test():
        events = []
        store = FakeStore(events)
        stored = _seed()
        # Existing releases stored the device ID in virtual_id only.
        stored.pop("device_id")
        stored["virtual_id"] = (b"device-id" + b"\x00" * 22)[:22].hex()
        store.devices["AA:BB:CC:DD:EE:FF"] = stored
        entry = _entry()
        entry.data = {"setup_method": "local"}
        flow = _new_config_flow(monkeypatch, FakeHass(events), entry, store)
        calls = []

        async def unexpected_cloud(*_args, **_kwargs):
            raise AssertionError("Known slot credentials should be read locally")

        async def activate(*_args, **kwargs):
            calls.append(kwargs["activation_seed"])

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected_cloud)
        monkeypatch.setattr(config_flow, "async_activate_lock", activate)
        result = await _discover_and_check(flow, _discovery("TyOS"))
        assert result["step_id"] == "confirm_local_activation"
        result = await flow.async_step_confirm_local_activation({})
        assert result == {"type": "abort", "reason": "device_added"}
        assert calls[0]["device_id"] == "device-id"

    asyncio.run(run_test())


@pytest.mark.parametrize("pair_fails", [False, True])
def test_reactivation_excludes_existing_coordinator_and_releases_lock(monkeypatch, pair_fails):
    async def run_test():
        events = []
        hass = FakeHass(events)
        store = FakeStore(events)
        entry = _entry()
        _install_common_fakes(monkeypatch, hass, store, _seed(), events)
        operation_lock = asyncio.Lock()

        class ExistingSession:
            async def async_disconnect(self):
                assert operation_lock.locked()
                events.append("existing-disconnect")

        coordinator = types.SimpleNamespace(
            _op_lock=operation_lock,
            _idle_timer=types.SimpleNamespace(cancel=lambda: events.append("cancel-idle")),
            _session=ExistingSession(),
        )
        entry.runtime_data = types.SimpleNamespace(coordinators={"AA:BB:CC:DD:EE:FF": coordinator})

        class NewSession(DisconnectableSession):
            def __init__(self, *_args, **_kwargs):
                assert operation_lock.locked()
                assert "existing-disconnect" in events

            async def async_pair_first_activation(self, _auth_key):
                assert operation_lock.locked()
                if pair_fails:
                    raise _SessionPairingFailedError()
                return b"abcdef", (b"device-id" + b"\x00" * 22)[:22]

        monkeypatch.setattr(activation, "TuyaBLELockSession", NewSession)
        try:
            await activation.async_activate_lock(
                hass, entry, address="AA:BB:CC:DD:EE:FF", activation_seed=_seed()
            )
        except activation.PairingFailedActivationError:
            assert pair_fails
        else:
            assert not pair_fails
        assert not operation_lock.locked()
        assert coordinator._idle_timer is None
        assert bool(store.devices) is not pair_fails

    asyncio.run(run_test())


def test_seed_storage_failure_prevents_activation_confirmation(monkeypatch):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)

        class FailingSeedStore:
            async def async_get_seed(self, _mac):
                return None

            async def async_save_seed(self, _mac, _seed):
                raise OSError("private storage path")

        monkeypatch.setattr(config_flow, "ActivationSeedStore", lambda _hass: FailingSeedStore())
        result = await _discover_and_check(flow, _discovery("TyOS"))
        assert result["step_id"] == "check_device"
        assert result["errors"] == {"base": "activation_storage_unavailable"}
        assert flow._activation_seed is None
        assert store.devices == {}

    asyncio.run(run_test())


def test_discovery_and_polling_do_not_fetch_keys_or_pair(monkeypatch):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("Discovery must wait for an explicit credential check")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected)
        monkeypatch.setattr(config_flow, "async_activate_lock", unexpected)
        result = await flow.async_step_bluetooth(_discovery("TyOS"))
        assert result["step_id"] == "check_device"
        assert result["errors"] == {}
        assert await flow.async_step_check_device() == result
        assert store.activation_seeds == {}
        assert store.devices == {}

    asyncio.run(run_test())


def test_failed_check_remains_visible_and_can_be_retried_before_pairing(monkeypatch):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)
        calls = []

        async def fetch(*_args, **_kwargs):
            calls.append("fetch")
            if calls.count("fetch") == 1:
                raise TimeoutError()
            return _seed()

        async def activate(*_args, **_kwargs):
            calls.append("pair")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", fetch)
        monkeypatch.setattr(config_flow, "async_activate_lock", activate)
        discovered = await flow.async_step_bluetooth(_discovery("TyOS"))
        assert discovered["step_id"] == "check_device"
        assert calls == []
        failed = await flow.async_step_check_device({})
        assert failed["step_id"] == "check_device"
        assert failed["errors"] == {"base": "cloud_fetch_failed"}
        checked = await flow.async_step_check_device({})
        assert checked["step_id"] == "confirm_new_device"
        assert checked["errors"] == {}
        assert calls == ["fetch", "fetch"]
        added = await flow.async_step_confirm_new_device({})
        assert added == {"type": "abort", "reason": "device_added"}
        assert calls == ["fetch", "fetch", "pair"]

    asyncio.run(run_test())


@pytest.mark.parametrize("name", [None, "AA:BB:CC:DD:EE:FF", "TyOS"])
def test_unbound_fd50_service_data_without_scan_response_is_discovered(monkeypatch, name):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)
        # Production packet: no name, manufacturer data, or service UUID list.
        discovery = _discovery(name)
        discovery.service_data = {
            "0000fd50-0000-1000-8000-00805f9b34fb": bytes.fromhex("510c0008626132716b313737")
        }
        discovery.service_uuids = []
        discovery.manufacturer_data = {}

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("Discovery must stay visible without cloud or pairing calls")

        monkeypatch.setattr(config_flow, "async_fetch_auth_key", unexpected)
        monkeypatch.setattr(config_flow, "async_activate_lock", unexpected)
        result = await flow.async_step_bluetooth(discovery)
        assert result["step_id"] == "check_device"
        assert result["errors"] == {}
        assert flow._pairing_mode_discovery is True
        assert store.devices == {}

    asyncio.run(run_test())


@pytest.mark.parametrize("name", [None, "TyOS"])
def test_bound_advertisement_uses_import_even_with_missing_or_stale_name(monkeypatch, name):
    async def run_test():
        events = []
        store = FakeStore(events)
        flow = _new_config_flow(monkeypatch, FakeHass(events), _entry(), store)
        store.activation_seeds["AA:BB:CC:DD:EE:FF"] = _seed()
        discovery = _discovery(name)
        discovery.service_data = {
            "0000fd50-0000-1000-8000-00805f9b34fb": bytes.fromhex("590c0008626132716b313737")
        }
        discovery.service_uuids = []
        discovery.manufacturer_data = {}
        result = await flow.async_step_bluetooth(discovery)
        assert flow._pairing_mode_discovery is False
        assert result == {"type": "abort", "reason": "device_added"}
        assert store.devices["AA:BB:CC:DD:EE:FF"]["local_key"] == "abcdefghijklmnop"

    asyncio.run(run_test())


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("", None),
        ("51", None),
        ("510c00086261", None),
        ("310c0008626132716b313737", None),
        ("510c0108626132716b313737", None),
        ("510c000f626132716b313737", None),
        ("410c0008626132716b313737", False),
        ("490c0008626132716b313737", True),
        ("510c0008626132716b313737", False),
        ("590c0008626132716b313737", True),
        ("5b0c0008626132716b313737", True),
    ],
)
def test_advertised_bind_state_requires_complete_supported_header(payload, expected):
    assert config_flow._advertised_bound_state(bytes.fromhex(payload)) is expected

@pytest.mark.parametrize('name', ['TyOS', 'Bound Lock'])
@pytest.mark.parametrize('category,pid,reason', [
    ('', 'ba2qk177', None),
    ('', 'unknown-product', 'device_type_unknown'),
    ('wg2', 'ba2qk177', 'not_a_lock'),
])
def test_missing_cloud_category_uses_exact_profile_only(monkeypatch, name, category, pid, reason):
    async def run():
        store = FakeStore([])
        flow = _new_config_flow(monkeypatch, FakeHass([]), _entry(), store)
        async def fetch(*args, **kwargs):
            return _seed(category=category, product_id=pid)
        monkeypatch.setattr(config_flow, 'async_fetch_auth_key', fetch)
        result = await _discover_and_check(flow, _discovery(name))
        if reason:
            expected = 'no_saved_locks' if name == 'Bound Lock' else reason
            assert result.get('reason') == expected or result.get('errors', {}).get('base') == expected
            assert not store.devices
        elif name == 'TyOS':
            assert result['step_id'] == 'confirm_new_device'
            assert store.activation_seeds
            assert not store.devices
        else:
            assert result['reason'] == 'device_added'
            assert store.devices
    asyncio.run(run())


def test_bound_lock_reimport_uses_saved_keys_without_account_login(monkeypatch):
    async def run():
        store = FakeStore([])
        store.activation_seeds['AA:BB:CC:DD:EE:FF'] = _seed(category='', product_id='ba2qk177')
        entry = _entry()
        entry.data = {}
        flow = _new_config_flow(monkeypatch, FakeHass([]), entry, store)
        async def unexpected(*args, **kwargs):
            raise AssertionError('Saved keys must permit cloud-free reimport')
        monkeypatch.setattr(config_flow, 'async_fetch_auth_key', unexpected)
        result = await _discover_and_check(flow, _discovery('Bound Lock'))
        assert result['reason'] == 'device_added'
    asyncio.run(run())


def test_discovery_never_logs_in_before_explicit_inventory_import(monkeypatch):
    async def run():
        flow = _new_config_flow(monkeypatch, FakeHass([]), _entry(), FakeStore([]))
        async def unexpected(*args, **kwargs):
            raise AssertionError('Radio discovery must not log into Tuya')
        monkeypatch.setattr(config_flow, 'async_fetch_auth_key', unexpected)
        monkeypatch.setattr(config_flow, 'async_sync_cloud_inventory', unexpected)
        result = await flow.async_step_bluetooth(_discovery('Bound Lock'))
        assert result['step_id'] == 'sync_cloud'
    asyncio.run(run())


def test_saved_inventory_lists_and_imports_sleeping_lock_without_cloud(monkeypatch):
    async def run():
        store = FakeStore([])
        flow = _new_config_flow(monkeypatch, FakeHass([]), _entry(), store)
        store.inventory = {
            'AA:BB:CC:DD:EE:01': _seed(name='Sleeping Lock', product_id='ba2qk177'),
            'AA:BB:CC:DD:EE:02': _seed(name='Gateway', category='wg2'),
        }
        async def unexpected(*args, **kwargs):
            raise AssertionError('Saved selection must not contact cloud or activate Bluetooth')
        monkeypatch.setattr(config_flow, 'async_fetch_auth_key', unexpected)
        monkeypatch.setattr(config_flow, 'async_activate_lock', unexpected)
        menu = await flow.async_step_user()
        assert menu['step_id'] == 'manage'
        form = await flow.async_step_saved_locks()
        assert form['step_id'] == 'saved_locks'
        assert form['description_placeholders']['count'] == '1'
        assert form['description_placeholders']['total'] == '2'
        result = await flow.async_step_saved_locks({'device_mac': 'AA:BB:CC:DD:EE:01'})
        assert result['reason'] == 'device_added'
        assert list(store.devices) == ['AA:BB:CC:DD:EE:01']
    asyncio.run(run())
