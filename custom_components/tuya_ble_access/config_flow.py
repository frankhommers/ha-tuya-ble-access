"""Config flow for Tuya BLE Smart Lock — hub-based architecture.

Creates one config entry backed by either a Tuya cloud account or locally
supplied credentials. Account inventories and keys are saved before Bluetooth use.
"""

from __future__ import annotations

import hashlib
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .activation import (
    BindVerificationActivationError,
    BluetoothUnavailableError,
    CloudFetchActivationError,
    DeviceAlreadyBoundActivationError,
    MissingActivationSeedError,
    PairingFailedActivationError,
    PostBindPersistenceActivationError,
    StorageUnavailableActivationError,
    async_activate_lock,
    validate_activation_seed,
)
from .const import (
    DOMAIN,
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_COUNTRY,
    CONF_TUYA_REGION,
    CONF_SETUP_METHOD,
    SETUP_METHOD_CLOUD,
    SETUP_METHOD_LOCAL,
    CONF_AUTH_KEY,
    CONF_AUTH_RANDOM,
    CONF_CHECK_CODE,
    CONF_DEVICE_MAC,
    CONF_DEVICE_UUID,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_SEC_KEY,
    CONF_VERIFY_KEY,
    LOCK_CATEGORIES,
)
from .device_profiles import async_get_profile_choices, async_resolve_category
from .device_store import ActivationSeedStore, DeviceStore, DeviceKeyRegistry
from .tuya_cloud import async_fetch_auth_key, async_sync_cloud_inventory

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_ID = "device_id"

_SECRET_TEXT = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD)
)

# Country → (country_code, region, display_name)
# Mappings from https://developer.tuya.com/en/docs/iot/oem-app-data-center-distributed
COUNTRY_OPTIONS: dict[str, tuple[str, str, str]] = {
    # Western America Data Center
    "us": ("1", "us", "United States"),
    "ca": ("1", "us", "Canada"),
    # Eastern America Data Center (mapped to "us" region API)
    "mx": ("52", "us", "México"),
    "br": ("55", "us", "Brasil"),
    "ar": ("54", "us", "Argentina"),
    "cl": ("56", "us", "Chile"),
    "co": ("57", "us", "Colombia"),
    "pe": ("51", "us", "Perú"),
    "ec": ("593", "us", "Ecuador"),
    "nz": ("64", "us", "New Zealand"),
    # Central Europe Data Center
    "nl": ("31", "eu", "Nederland"),
    "be": ("32", "eu", "België"),
    "de": ("49", "eu", "Deutschland"),
    "fr": ("33", "eu", "France"),
    "gb": ("44", "eu", "United Kingdom"),
    "es": ("34", "eu", "España"),
    "it": ("39", "eu", "Italia"),
    "pt": ("351", "eu", "Portugal"),
    "at": ("43", "eu", "Österreich"),
    "ch": ("41", "eu", "Schweiz"),
    "se": ("46", "eu", "Sverige"),
    "no": ("47", "eu", "Norge"),
    "dk": ("45", "eu", "Danmark"),
    "fi": ("358", "eu", "Suomi"),
    "pl": ("48", "eu", "Polska"),
    "ie": ("353", "eu", "Ireland"),
    "cz": ("420", "eu", "Česko"),
    "ro": ("40", "eu", "România"),
    "hu": ("36", "eu", "Magyarország"),
    "gr": ("30", "eu", "Ελλάδα"),
    "tr": ("90", "eu", "Türkiye"),
    "il": ("972", "eu", "Israel"),
    "za": ("27", "eu", "South Africa"),
    "au": ("61", "eu", "Australia"),
    "ru": ("7", "eu", "Russia"),
    "ua": ("380", "eu", "Ukraine"),
    "eg": ("20", "eu", "Egypt"),
    "ng": ("234", "eu", "Nigeria"),
    "ke": ("254", "eu", "Kenya"),
    "pk": ("92", "eu", "Pakistan"),
    "bd": ("880", "eu", "Bangladesh"),
    "lk": ("94", "eu", "Sri Lanka"),
    "np": ("977", "eu", "Nepal"),
    "sa": ("966", "eu", "Saudi Arabia"),
    "ae": ("971", "eu", "United Arab Emirates"),
    "qa": ("974", "eu", "Qatar"),
    "kw": ("965", "eu", "Kuwait"),
    "jp": ("81", "eu", "日本"),
    "kr": ("82", "eu", "대한민국"),
    "mn": ("976", "eu", "Mongolia"),
    # Singapore Data Center
    "sg": ("65", "eu", "Singapore"),
    "my": ("60", "eu", "Malaysia"),
    "th": ("66", "eu", "Thailand"),
    "id": ("62", "eu", "Indonesia"),
    "ph": ("63", "eu", "Philippines"),
    "vn": ("84", "eu", "Việt Nam"),
    "mm": ("95", "eu", "Myanmar"),
    "kh": ("855", "eu", "Cambodia"),
    "la": ("856", "eu", "Laos"),
    "bn": ("673", "eu", "Brunei"),
    "hk": ("852", "eu", "Hong Kong"),
    "tw": ("886", "eu", "Taiwan"),
    # India Data Center
    "in": ("91", "in", "India"),
    # China Data Center
    "cn": ("86", "cn", "中国"),
    "other": ("", "", "Other (manual)"),
}

# Region dropdown options (display_name → api_value)
REGION_OPTIONS: dict[str, str] = {
    "Europe (EU)": "eu",
    "Americas (US)": "us",
    "China (CN)": "cn",
    "India (IN)": "in",
}


def _country_choices() -> dict[str, str]:
    """Sorted country choices for dropdown."""
    return {k: v[2] for k, v in sorted(COUNTRY_OPTIONS.items(), key=lambda x: x[1][2])}


def _build_cloud_schema(selected_country: str | None = None) -> vol.Schema:
    """Build the login schema based on selected country."""
    country = selected_country or "nl"

    # If "other", show manual country code + region dropdown
    if country == "other":
        return vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("country_code"): str,
                vol.Required("region", default="eu"): _region_selector(),
            }
        )

    # Normal country — region is auto-detected, only email + password needed
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _region_selector():
    return SelectSelector(SelectSelectorConfig(
        options=list(REGION_OPTIONS.values()), translation_key="region",
    ))


def _advertised_bound_state(service_data: bytes) -> bool | None:
    """Read the bind bit in a complete Tuya V4/V5 FD50 PID advertisement.

    The local name/manufacturer data can be absent when scan responses are lost.
    Tuya documents control bit 3 as bound (0x41 -> 0x49 for V4); captured K3 V5
    packets use the same bit (0x51 -> 0x59).
    """
    if (
        len(service_data) < 12
        or service_data[0] >> 4 not in (4, 5)
        or service_data[2] != 0  # PID product identifier
        or service_data[3] != 8
    ):
        return None
    return bool(service_data[0] & 0x08)


def _decrypt_uuid(service_data: bytes, encrypted_id: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.md5(service_data).digest()
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    return (dec.update(encrypted_id) + dec.finalize()).decode("ascii").rstrip("\x00")


def _normalize_local_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "")
    if len(compact) != 12:
        raise ValueError("invalid MAC address")
    try:
        bytes.fromhex(compact)
    except ValueError as exc:
        raise ValueError("invalid MAC address") from exc
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).upper()


def _local_ascii(value: object, field: str, *, length: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ASCII string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be an ASCII string") from exc
    if length is not None and len(encoded) != length:
        raise ValueError(f"{field} must be exactly {length} ASCII bytes")
    return value


def _local_hex(value: object, field: str, byte_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be hexadecimal") from exc
    if len(decoded) != byte_length:
        raise ValueError(f"{field} must be {byte_length}-byte hex")
    return value.lower()


def _local_device_record(user_input: dict) -> tuple[str, dict]:
    """Validate locally supplied credentials and build a device-store record."""
    mac = _normalize_local_mac(user_input[CONF_DEVICE_MAC])
    local_key = _local_ascii(user_input[CONF_LOCAL_KEY], CONF_LOCAL_KEY, length=16)
    sec_key = _local_ascii(user_input[CONF_SEC_KEY], CONF_SEC_KEY, length=16)
    device_id = _local_ascii(user_input[CONF_DEVICE_ID], CONF_DEVICE_ID)
    device_uuid = _local_ascii(user_input[CONF_DEVICE_UUID], CONF_DEVICE_UUID)
    check_code = user_input[CONF_CHECK_CODE]
    if not isinstance(check_code, str) or len(check_code) != 8 or not check_code.isdigit():
        raise ValueError("check_code must contain exactly 8 digits")

    login_key = local_key.encode("ascii")[:6]
    virtual_id = (device_id.encode("ascii") + b"\x00" * 22)[:22]
    return mac, {
        "uuid": device_uuid,
        "login_key": login_key.hex(),
        "virtual_id": virtual_id.hex(),
        "auth_key": _local_hex(user_input[CONF_AUTH_KEY], CONF_AUTH_KEY, 16),
        "auth_random": _local_hex(
            user_input[CONF_AUTH_RANDOM], CONF_AUTH_RANDOM, 16
        ),
        "product_id": user_input[CONF_PRODUCT_ID],
        "name": user_input[CONF_NAME].strip() or mac,
        "local_key": local_key,
        "sec_key": sec_key,
        "verify_key": _local_hex(
            user_input[CONF_VERIFY_KEY], CONF_VERIFY_KEY, 4
        ),
        "check_code": check_code,
        "cloud_dps": {},
    }


class TuyaBLELockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self):
        self._mac = None
        self._name = None
        self._uuid = None
        self._email = None
        self._password = None
        self._country = None
        self._region = None
        self._selected_country = None
        self._activation_entry_id = None
        self._pairing_mode_discovery = False
        self._activation_seed = None
        self._activation_account = None
        self._activation_source = None
        self._activation_existing_record = None

    # ---- BLE discovery ----

    async def async_step_bluetooth(self, discovery_info):
        if not self._async_current_entries() and self.hass.config_entries.async_entries("tuya_ble_lock"):
            return await self.async_step_migrate()
        self._mac = format_mac(discovery_info.address).upper()
        self._name = discovery_info.name or self._mac
        self._pairing_mode_discovery = (self._name or "").casefold() == "tyos"
        await self.async_set_unique_id(format_mac(self._mac))
        self._abort_if_unique_id_configured()

        # Try decrypt UUID from FD50 service data
        svc_data = None
        for suuid, sd in (discovery_info.service_data or {}).items():
            if "fd50" in suuid.lower():
                svc_data = sd
                break
        advertised_bound = _advertised_bound_state(bytes(svc_data or b""))
        if advertised_bound is not None:
            self._pairing_mode_discovery = not advertised_bound

        man = discovery_info.manufacturer_data.get(0x07D0)
        if svc_data and man and len(man) >= 20:
            try:
                self._uuid = _decrypt_uuid(bytes(svc_data), bytes(man[4:20]))
            except Exception:
                self._uuid = None

        # Check if already known in device store
        existing_entries = self._async_current_entries()
        if existing_entries:
            try:
                known = (await DeviceKeyRegistry(self.hass).async_inventory()).get(self._mac)
            except Exception:
                return self.async_abort(reason="activation_storage_unavailable")
            if known:
                category = await async_resolve_category(self.hass, known)
                if category and category not in LOCK_CATEGORIES:
                    return self.async_abort(reason="not_a_lock")
            entry = existing_entries[0]
            self._activation_entry_id = entry.entry_id
            device_store = DeviceStore(self.hass)
            await device_store.async_load()
            if device_store.get_device(self._mac) and not self._pairing_mode_discovery:
                return self.async_abort(reason="already_configured")
            if self._pairing_mode_discovery:
                return await self.async_step_check_device()
            # Hub exists — try auto-add using stored creds
            return await self._async_auto_add_device(entry, device_store)

        # No hub entry yet — start cloud login
        return await self.async_step_select_country()

    async def async_step_check_device(self, user_input=None):
        """Keep discovery visible; only check keys after an explicit user action."""
        errors = {}
        if user_input is not None:
            result = await self.async_step_confirm_new_device()
            if result.get("type") != "abort":
                return result
            # A failed check must remain visible and retryable, not consume discovery.
            errors["base"] = result["reason"]
        return self.async_show_form(
            step_id="check_device",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": self._name, "mac": self._mac},
        )

    async def _async_auto_add_device(self, entry, device_store):
        """Import a known bound device from saved keys; otherwise offer account import."""
        self._activation_entry_id = entry.entry_id
        try:
            cached = await ActivationSeedStore(self.hass).async_get_seed(self._mac)
        except Exception:
            return self.async_abort(reason="activation_storage_unavailable")
        cloud_result = None
        if cached:
            try:
                cloud_result = validate_activation_seed(cached, self._uuid or "")
            except MissingActivationSeedError:
                pass
        if cloud_result is None:
            # A Bluetooth packet must never trigger an account login. Offer an
            # explicit account import; it collects all types and keys in one session.
            return await self.async_step_sync_cloud()

        if not cloud_result.get("device_id"):
            return self.async_abort(reason="pair_in_app")

        # The BLE matcher keys on the FD50 service UUID, which every Tuya BLE
        # device advertises -- gateways included. Only adopt actual locks, or a
        # SigMesh gateway sitting next to the lock gets adopted as one.
        category = await async_resolve_category(self.hass, cloud_result)
        if category not in LOCK_CATEGORIES:
            # A cloud category or exact product profile must identify a lock.
            # Generic Tuya advertisements and the default profile are insufficient.
            _LOGGER.debug(
                "Not auto-adding %s: category %r is not a known lock",
                self._mac, category or "(unknown)",
            )
            return self.async_abort(reason="not_a_lock" if category else "device_type_unknown")

        auth_key = cloud_result.get("auth_key", "")
        local_key = cloud_result.get("local_key", "")
        device_id = cloud_result.get("device_id", "")
        product_id = cloud_result.get("product_id", "")
        name = cloud_result.get("name") or self._name or self._mac
        uuid = cloud_result.get("uuid") or self._uuid or ""

        if local_key and device_id:
            # Device already bound to Tuya account — derive BLE credentials
            login_key = local_key[:6].encode()
            virtual_id = (device_id.encode() + b"\x00" * 22)[:22]
            await device_store.async_add_device(
                self._mac,
                {
                    "uuid": uuid,
                    "login_key": login_key.hex(),
                    "virtual_id": virtual_id.hex(),
                    "auth_key": auth_key,
                    "auth_random": cloud_result.get("auth_random", ""),
                    "product_id": product_id,
                    "category": category,
                    "name": name,
                    "local_key": local_key,
                    "sec_key": cloud_result.get("sec_key", ""),
                    "verify_key": cloud_result.get("verify_key", ""),
                    "check_code": cloud_result.get("check_code", ""),
                    "cloud_dps": cloud_result.get("dps") or {},
                },
            )
            _LOGGER.info("Auto-added device %s (%s) to hub", name, self._mac)
            # Reload entry to pick up new device
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="device_added")

        # Missing account credentials are not evidence that BLE activation is possible.
        return self.async_abort(reason="pair_in_app")

    @staticmethod
    def _account_identity(entry):
        return tuple(entry.data.get(key, "") for key in (
            CONF_TUYA_EMAIL, CONF_TUYA_PASSWORD, CONF_TUYA_COUNTRY, CONF_TUYA_REGION
        ))

    async def _async_prepare_activation(self, entry):
        """Check account membership and keys without changing Bluetooth pairing."""
        self._activation_seed = None
        self._activation_account = None
        self._activation_source = None
        try:
            seeds = ActivationSeedStore(self.hass)
            devices = DeviceStore(self.hass)
            await devices.async_load()
            # Active-device credentials take precedence over an older cached seed.
            existing = devices.get_device(self._mac)
            self._activation_existing_record = dict(existing) if existing else None
            candidates = [existing, await seeds.async_get_seed(self._mac)]
        except Exception:
            return self.async_abort(reason="activation_storage_unavailable")
        for candidate in candidates:
            if not candidate:
                continue
            try:
                self._activation_seed = validate_activation_seed(candidate, self._uuid or "")
            except MissingActivationSeedError:
                continue
            self._activation_source = "local"
            return None
        email, password, country, region = self._account_identity(entry)
        if not email or not password:
            return self.async_abort(reason="missing_credentials")
        try:
            inventory = await async_sync_cloud_inventory(self.hass, email, password, country, region)
            cloud = inventory.get(self._mac.upper(), {})
        except Exception:
            return self.async_abort(reason="cloud_fetch_failed")
        if not cloud.get("device_id"):
            return self.async_abort(reason="pair_in_app")
        category = await async_resolve_category(self.hass, cloud)
        if category not in LOCK_CATEGORIES:
            return self.async_abort(reason="not_a_lock" if category else "device_type_unknown")
        try:
            self._activation_seed = validate_activation_seed(cloud, self._uuid or "")
        except MissingActivationSeedError:
            return self.async_abort(reason="activation_seed_missing")
        try:
            await seeds.async_save_seed(self._mac, self._activation_seed)
        except Exception:
            self._activation_seed = None
            return self.async_abort(reason="activation_storage_unavailable")
        self._activation_source = "cloud"
        self._activation_account = self._account_identity(entry)
        return None

    async def async_step_confirm_new_device(self, user_input=None):
        """Confirm adding a new lock that needs BLE pairing."""
        errors = {}
        entry = next(
            (current_entry for current_entry in self._async_current_entries()
             if current_entry.entry_id == self._activation_entry_id),
            None,
        )
        if entry is not None and (
            self._activation_seed is None
            or (self._activation_source == "cloud"
                and self._activation_account != self._account_identity(entry))
        ):
            result = await self._async_prepare_activation(entry)
            if result is not None:
                return result
            # A changed account or an unprepared direct submit must be confirmed again.
            user_input = None
        if user_input is not None:
            if entry is None:
                errors["base"] = "integration_changed"
            else:
                device_store = DeviceStore(self.hass)
                try:
                    await device_store.async_load()
                except Exception as exc:
                    _LOGGER.warning(
                        "Could not reload device store before activation of %s (%s)",
                        self._mac,
                        type(exc).__name__,
                    )
                    errors["base"] = "activation_storage_unavailable"
                else:
                    current_record = device_store.get_device(self._mac)
                    if current_record and current_record != self._activation_existing_record:
                        return self.async_abort(reason="already_configured")
                    try:
                        await async_activate_lock(
                            self.hass,
                            entry,
                            address=self._mac,
                            device_uuid=self._uuid or "",
                            name=self._name or "",
                            activation_seed=self._activation_seed,
                        )
                    except MissingActivationSeedError:
                        errors["base"] = "activation_seed_missing"
                    except BluetoothUnavailableError:
                        errors["base"] = "bluetooth_unavailable"
                    except DeviceAlreadyBoundActivationError:
                        errors["base"] = "device_already_bound"
                    except PairingFailedActivationError:
                        errors["base"] = "pairing_failed"
                    except BindVerificationActivationError:
                        errors["base"] = "bind_verification_failed"
                    except CloudFetchActivationError:
                        errors["base"] = "activation_cloud_fetch_failed"
                    except StorageUnavailableActivationError:
                        errors["base"] = "activation_storage_unavailable"
                    except PostBindPersistenceActivationError:
                        errors["base"] = "activation_credentials_not_saved"
                    except Exception as exc:
                        _LOGGER.warning(
                            "Local BLE activation failed for %s (%s)",
                            self._mac,
                            type(exc).__name__,
                        )
                        errors["base"] = "activation_failed"
                    else:
                        return self.async_abort(reason="device_added")
            if errors:
                _LOGGER.warning(
                    "Local BLE activation did not complete for %s (%s)",
                    self._mac,
                    errors["base"],
                )
        return self.async_show_form(
            step_id=("confirm_local_activation" if self._activation_source == "local"
                     else "confirm_new_device"),
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": self._name, "mac": self._mac},
        )

    async def async_step_confirm_local_activation(self, user_input=None):
        """Confirm reactivation with locally saved keys, without cloud access."""
        return await self.async_step_confirm_new_device(user_input)

    # ---- Manual setup (first hub creation) ----

    async def async_step_user(self, user_input=None):
        """Manual setup entry point."""
        if self._async_current_entries():
            return self.async_show_menu(step_id="manage", menu_options=["saved_locks", "sync_cloud"])
        if self.hass.config_entries.async_entries("tuya_ble_lock"):
            return await self.async_step_migrate()
        return self.async_show_menu(
            step_id="user",
            menu_options=[SETUP_METHOD_CLOUD, SETUP_METHOD_LOCAL],
        )

    async def async_step_sync_cloud(self, user_input=None):
        """Explicit account-wide synchronization, never triggered by radio discovery."""
        entries = self._async_current_entries()
        if not entries:
            return await self.async_step_cloud()
        entry = entries[0]
        email, password, country, region = self._account_identity(entry)
        if not email or not password:
            return self.async_abort(reason="missing_credentials")
        errors = {}
        if user_input is not None:
            try:
                await async_sync_cloud_inventory(self.hass, email, password, country, region)
            except Exception:
                errors["base"] = "cloud_fetch_failed"
            else:
                return await self.async_step_saved_locks()
        return self.async_show_form(step_id="sync_cloud", data_schema=vol.Schema({}), errors=errors)

    async def async_step_saved_locks(self, user_input=None):
        """Select a saved cloud lock even when it is asleep or out of range."""
        errors = {}
        try:
            records = await DeviceKeyRegistry(self.hass).async_inventory()
        except Exception:
            return self.async_abort(reason="activation_storage_unavailable")
        active = DeviceStore(self.hass)
        await active.async_load()
        choices = {}
        summary = []
        for mac, record in records.items():
            category = await async_resolve_category(self.hass, record)
            try:
                validate_activation_seed(record)
                key_status = "✓"
            except MissingActivationSeedError:
                key_status = "?"
            summary.append(f"{record.get('name') or mac} | {category or '?'} | {record.get('product_id') or '?'} | {mac} | 🔑 {key_status}")
            if category not in LOCK_CATEGORIES or active.get_device(mac):
                continue
            choices[mac] = f"{record.get('name') or mac} — {record.get('product_id') or category} — {mac}"
        if user_input is not None:
            mac = user_input.get(CONF_DEVICE_MAC)
            if mac not in choices:
                errors["base"] = "device_type_unknown"
            else:
                record = records[mac]
                try:
                    seed = validate_activation_seed(record)
                except MissingActivationSeedError:
                    errors["base"] = "activation_seed_missing"
                else:
                    observed_unbound = self._pairing_mode_discovery and self._mac == mac
                    self._mac, self._name, self._uuid = mac, record.get("name") or mac, seed["uuid"]
                    # Never infer a reset from absence: only a received advertisement
                    # can establish pairing mode. Activation always needs confirmation.
                    self._pairing_mode_discovery = observed_unbound
                    await ActivationSeedStore(self.hass).async_save_seed(mac, seed)
                    entries = self._async_current_entries()
                    if entries:
                        if observed_unbound:
                            self._activation_entry_id = entries[0].entry_id
                            return await self.async_step_check_device()
                        return await self._async_auto_add_device(entries[0], active)
                    return await self._create_hub_with_device(seed)
        if not choices:
            # A new hub can be saved even when the account has no importable locks.
            if not self._async_current_entries() and self._email:
                return await self._create_hub_entry()
            return self.async_abort(reason="no_saved_locks")
        return self.async_show_form(
            step_id="saved_locks",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_MAC): vol.In(choices)}),
            errors=errors,
            description_placeholders={"count": str(len(choices)), "total": str(len(records)), "inventory": "\n\n".join(summary)},
        )

    async def async_step_migrate(self, user_input=None):
        """Confirm taking over the existing hub without re-enrolling the lock."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        entries = self.hass.config_entries.async_entries("tuya_ble_lock")
        if len(entries) != 1 or entries[0].version != 2 or entries[0].disabled_by:
            return self.async_abort(reason="legacy_migration_unavailable")
        source = entries[0]
        if user_input is None:
            return self.async_show_form(step_id="migrate", data_schema=vol.Schema({}))
        await self.async_set_unique_id(source.unique_id or "tuya_ble_access_hub")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Tuya BLE Access",
            data={**source.data, "legacy_domain_migration": {"entry_id": source.entry_id}},
            options=dict(source.options),
        )

    async def async_step_cloud(self, user_input=None):
        """Set up a hub using a Tuya cloud account."""
        return await self.async_step_select_country()

    async def async_step_local(self, user_input=None):
        """Set up an already-bound lock from local BLE credentials."""
        errors: dict[str, str] = {}
        profile_choices = await async_get_profile_choices(self.hass)

        if user_input is not None:
            try:
                mac, record = _local_device_record(user_input)
            except (KeyError, ValueError):
                errors["base"] = "invalid_local_credentials"
            else:
                await self.async_set_unique_id("tuya_ble_access_local")
                self._abort_if_unique_id_configured()
                device_store = DeviceStore(self.hass)
                try:
                    await device_store.async_load()
                    await device_store.async_add_device(mac, record)
                except Exception:
                    _LOGGER.warning(
                        "Could not persist locally imported lock %s",
                        mac,
                        exc_info=True,
                    )
                    errors["base"] = "local_storage_unavailable"
                else:
                    return self.async_create_entry(
                        title="Tuya BLE Access",
                        data={CONF_SETUP_METHOD: SETUP_METHOD_LOCAL},
                    )

        profile_key = vol.Required(CONF_PRODUCT_ID)
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=self._name or "Tuya BLE Access"): str,
                    vol.Required(CONF_DEVICE_MAC, default=self._mac or ""): str,
                    vol.Required(CONF_DEVICE_UUID, default=self._uuid or ""): str,
                    vol.Required(CONF_DEVICE_ID): str,
                    profile_key: vol.In(profile_choices),
                    vol.Required(CONF_LOCAL_KEY): _SECRET_TEXT,
                    vol.Required(CONF_SEC_KEY): _SECRET_TEXT,
                    vol.Required(CONF_VERIFY_KEY): _SECRET_TEXT,
                    vol.Required(CONF_AUTH_KEY): _SECRET_TEXT,
                    vol.Required(CONF_AUTH_RANDOM): _SECRET_TEXT,
                    vol.Required(CONF_CHECK_CODE): _SECRET_TEXT,
                }
            ),
            errors=errors,
        )

    # ---- Step 1: Country selection ----

    async def async_step_select_country(self, user_input=None):
        """Select your country to auto-configure region and country code."""
        errors = {}

        if user_input:
            self._selected_country = user_input["country"]
            info = COUNTRY_OPTIONS.get(self._selected_country)

            if self._selected_country == "other":
                return await self.async_step_cloud_login()

            if info:
                self._country = info[0]
                self._region = info[1]
                return await self.async_step_cloud_login()

            errors["base"] = "invalid_country"

        return self.async_show_form(
            step_id="select_country",
            data_schema=vol.Schema(
                {
                    vol.Required("country", default="nl"): SelectSelector(
                        SelectSelectorConfig(
                            options=list(COUNTRY_OPTIONS), translation_key="country",
                            sort=True,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ---- Step 2: Login ----

    async def async_step_cloud_login(self, user_input=None):
        """Enter Tuya credentials. Region is auto-filled or shown as dropdown for 'Other'."""
        errors = {}

        if user_input:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            # Handle "other" country — resolve region dropdown
            if self._selected_country == "other":
                self._country = user_input.get("country_code", "")
                region_display = user_input.get("region", "eu")
                self._region = REGION_OPTIONS.get(region_display, region_display)

            # Validate credentials by attempting a cloud call
            try:
                await async_sync_cloud_inventory(
                    self.hass, self._email, self._password, self._country, self._region,
                )
                return await self.async_step_saved_locks()
            except Exception:
                _LOGGER.warning("Cloud login failed", exc_info=True)
                errors["base"] = "auth_key_failed"

        schema = _build_cloud_schema(self._selected_country)
        return self.async_show_form(
            step_id="cloud_login",
            data_schema=schema,
            errors=errors,
            description_placeholders={"region": self._region or "—"},
        )

    async def _create_hub_with_device(self, cloud_result: dict):
        """Create the hub entry and add the first bound device."""
        if not cloud_result.get("device_id"):
            return self.async_abort(reason="pair_in_app")
        if self._pairing_mode_discovery:
            category = await async_resolve_category(self.hass, cloud_result)
            if category not in LOCK_CATEGORIES:
                return self.async_abort(reason="not_a_lock" if category else "device_type_unknown")
            try:
                validate_activation_seed(cloud_result, self._uuid or "")
            except MissingActivationSeedError:
                return self.async_abort(reason="activation_seed_missing")
            return await self._create_hub_entry()

        # The BLE matcher keys on the FD50 service UUID, which every Tuya BLE
        # device advertises -- gateways included. Only adopt actual locks, or a
        # SigMesh gateway sitting next to the lock gets adopted as one.
        category = await async_resolve_category(self.hass, cloud_result)
        if category not in LOCK_CATEGORIES:
            _LOGGER.debug(
                "Not adopting %s as a lock: category %r",
                self._mac, category or "(unknown)",
            )
            return await self._create_hub_entry()

        auth_key = cloud_result.get("auth_key", "")
        local_key = cloud_result.get("local_key", "")
        device_id = cloud_result.get("device_id", "")
        product_id = cloud_result.get("product_id", "")
        name = cloud_result.get("name") or self._name or self._mac
        uuid = cloud_result.get("uuid") or self._uuid or ""

        # Save device to store
        device_store = DeviceStore(self.hass)
        await device_store.async_load()

        if local_key and device_id:
            login_key = local_key[:6].encode()
            virtual_id = (device_id.encode() + b"\x00" * 22)[:22]
            await device_store.async_add_device(
                self._mac,
                {
                    "uuid": uuid,
                    "login_key": login_key.hex(),
                    "virtual_id": virtual_id.hex(),
                    "auth_key": auth_key,
                    "auth_random": cloud_result.get("auth_random", ""),
                    "product_id": product_id,
                    "category": category,
                    "name": name,
                    "local_key": local_key,
                    "sec_key": cloud_result.get("sec_key", ""),
                    "verify_key": cloud_result.get("verify_key", ""),
                    "check_code": cloud_result.get("check_code", ""),
                    "cloud_dps": cloud_result.get("dps") or {},
                },
            )

        return await self._create_hub_entry()

    async def _create_hub_entry(self):
        """Create the single hub config entry with cloud credentials."""
        await self.async_set_unique_id(self._email)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Tuya BLE Access",
            data={
                CONF_SETUP_METHOD: SETUP_METHOD_CLOUD,
                CONF_TUYA_EMAIL: self._email,
                CONF_TUYA_PASSWORD: self._password,
                CONF_TUYA_COUNTRY: self._country,
                CONF_TUYA_REGION: self._region,
            },
        )

    # ---- Reauth / Reconfigure ----
    #
    # HA exposes two entry points that do almost the same thing:
    #   * async_step_reauth         — triggered automatically when a call
    #                                 raises ConfigEntryAuthFailed
    #   * async_step_reconfigure    — manual "Reconfigure" option in the
    #                                 hub's ⋮ menu (HA 2024.11+)
    # Both land on the same confirm form.

    async def _prime_from_entry(self) -> None:
        entry = self._reconfigure_source_entry()
        data = (entry.data if entry else {}) or {}
        self._email = data.get(CONF_TUYA_EMAIL, "")
        self._country = data.get(CONF_TUYA_COUNTRY, "")
        self._region = data.get(CONF_TUYA_REGION, "")

    def _reconfigure_source_entry(self):
        entry_id = self.context.get("entry_id", "")
        if entry_id:
            return self.hass.config_entries.async_get_entry(entry_id)
        return None

    async def async_step_reauth(self, entry_data: dict):
        """Automatic reauth (e.g. password invalidated)."""
        self._email = entry_data.get(CONF_TUYA_EMAIL, "")
        self._country = entry_data.get(CONF_TUYA_COUNTRY, "")
        self._region = entry_data.get(CONF_TUYA_REGION, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reconfigure(self, user_input=None):
        """Manual 'Reconfigure' entry (HA hub ⋮ menu).

        Prompts for email and password (email prepopulated) so the Tuya
        account can be (re)supplied. A hub that was set up locally never stored
        cloud credentials, so country/region are asked too; on success the
        entry is upgraded to a cloud-backed one. This is the supported path
        after a re-pair in the Tuya app rotates local_key/sec_key/check_code:
        it pulls the fresh credentials down and, from then on, the
        'Refresh status via cloud' button works with a single press.
        """
        from .tuya_cloud import async_refresh_all_devices

        await self._prime_from_entry()
        entry = self._reconfigure_source_entry()
        # A local hub stored no country/region — collect whatever is missing.
        need_country = not self._country
        need_region = not self._region
        errors: dict[str, str] = {}

        if user_input is not None:
            new_email = user_input[CONF_EMAIL]
            new_password = user_input[CONF_PASSWORD]
            country = user_input.get(CONF_TUYA_COUNTRY) or self._country
            region = user_input.get(CONF_TUYA_REGION) or self._region
            # _entry_creds reads email/country/region off the entry, so they
            # must be in place before the refresh runs. Password is passed in.
            if entry:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_TUYA_EMAIL: new_email,
                        CONF_TUYA_COUNTRY: country,
                        CONF_TUYA_REGION: region,
                    },
                )
                self._email, self._country, self._region = (
                    new_email, country, region,
                )
            try:
                refreshed = await async_refresh_all_devices(
                    self.hass, entry, new_password=new_password,
                ) if entry else 0
            except Exception as exc:
                _LOGGER.warning("Reconfigure refresh failed: %s", exc)
                errors["base"] = "auth_key_failed"
            else:
                _LOGGER.info(
                    "Reconfigure complete: refreshed %d device(s)", refreshed
                )
                if entry:
                    # Persist the working credentials and mark the hub
                    # cloud-backed so the cloud-refresh button works later.
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={
                            **entry.data,
                            CONF_SETUP_METHOD: SETUP_METHOD_CLOUD,
                            CONF_TUYA_PASSWORD: new_password,
                        },
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        fields: dict = {
            vol.Required(CONF_EMAIL, default=self._email or ""): str,
            vol.Required(CONF_PASSWORD): str,
        }
        if need_country:
            fields[vol.Required(CONF_TUYA_COUNTRY, default="31")] = str
        if need_region:
            fields[vol.Required(CONF_TUYA_REGION, default="eu")] = _region_selector()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for password (and country/region if missing), re-login, refresh
        BLE credentials for every device in the store.

        Use cases:
          * Tuya password changed.
          * Lock was re-paired in the Tuya app: localKey/secKey/check_code
            all rotated and need to be pulled down again.
        """
        from .tuya_cloud import async_refresh_all_devices
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )

        if user_input:
            password = user_input[CONF_PASSWORD]
            country = user_input.get(CONF_TUYA_COUNTRY) or self._country
            region = user_input.get(CONF_TUYA_REGION) or self._region
            # Temporarily patch country/region if the user had to supply them
            if entry and (country != entry.data.get(CONF_TUYA_COUNTRY) or
                          region != entry.data.get(CONF_TUYA_REGION)):
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_TUYA_COUNTRY: country,
                          CONF_TUYA_REGION: region},
                )
            try:
                refreshed = await async_refresh_all_devices(
                    self.hass, entry, new_password=password,
                ) if entry else 0
            except Exception as exc:
                _LOGGER.warning("Reauth refresh failed: %s", exc)
                errors["base"] = "auth_key_failed"
            else:
                _LOGGER.info("Reauth complete: refreshed %d device(s)", refreshed)
                if entry:
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        # Build schema: password always, country/region only if missing
        fields: dict = {vol.Required(CONF_PASSWORD): str}
        if not self._country:
            fields[vol.Required(CONF_TUYA_COUNTRY, default="31")] = str
        if not self._region:
            fields[vol.Required(CONF_TUYA_REGION, default="eu")] = _region_selector()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )
