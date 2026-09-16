"""Service handlers for Tuya BLE lock integration."""

from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import service as service_helper

from .activation import ActivationError, async_activate_lock
from .const import (
    DOMAIN,
    CRED_PASSWORD,
    CRED_FINGERPRINT,
    CRED_CARD,
)
from .credential_store import CredentialStore
from .credential_time import CredentialTimeError, parse_credential_window
from .ble_commands import (
    build_enroll_payload,
    build_delete_payload,
    build_temp_password_payload,
    parse_temp_password_response,
    parse_enroll_response,
    parse_credential_list,
    SYNC_MARKER,
)
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)
_MAC_ADDRESS_RE = re.compile(
    r"(?:[0-9a-fA-F]{12}"
    r"|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"
    r"|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})"
)
_CORE_BLUETOOTH_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _normalize_bluetooth_address(address: str) -> str:
    """Validate and normalize a MAC address or CoreBluetooth UUID."""
    if not address.strip():
        raise vol.Invalid("Bluetooth address must not be empty")
    if _MAC_ADDRESS_RE.fullmatch(address):
        compact = address.replace(":", "").replace("-", "")
        return ":".join(
            compact[index:index + 2] for index in range(0, 12, 2)
        ).upper()
    if _CORE_BLUETOOTH_UUID_RE.fullmatch(address):
        return address.upper()
    raise vol.Invalid(
        "Bluetooth address must be a MAC address or CoreBluetooth UUID"
    )


ACTIVATE_SCHEMA = vol.Schema({
    vol.Required("address"): vol.All(str, _normalize_bluetooth_address),
    vol.Optional("device_uuid"): str,
    vol.Optional("name"): str,
})

ADD_PIN_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Required("pin_code"): str,
    vol.Optional("admin", default=False): bool,
})

ADD_FINGERPRINT_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("finger"): vol.Any(str, None),
    vol.Optional("admin", default=False): bool,
})

ADD_CARD_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("admin", default=False): bool,
})

CRED_TYPE_NAMES = {"pin": CRED_PASSWORD, "card": CRED_CARD, "fingerprint": CRED_FINGERPRINT}
CRED_TYPE_LABELS = {CRED_PASSWORD: "pin", CRED_CARD: "card", CRED_FINGERPRINT: "fingerprint", 0x04: "face"}

DELETE_CREDENTIAL_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("credential_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Optional("cred_type"): vol.Any("pin", "card", "fingerprint", None),
})

LIST_CREDENTIALS_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
})

CLEAR_CREDENTIALS_SCHEMA = vol.Schema({
    vol.Optional("device_id"): str,
})

REPORT_FACTORY_RESET_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
})

SYNC_CREDENTIALS_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
})

CLEAR_FINGERPRINTS_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("hw_id"): int,
})

REGISTER_CREDENTIAL_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("person"): vol.Any(str, None),
    vol.Optional("member_name"): vol.Any(str, None),
    vol.Required("cred_type"): vol.In(tuple(CRED_TYPE_NAMES)),
    vol.Required("hw_id"): int,
    vol.Optional("name"): vol.Any(str, None),
})

PROBE_RECORDS_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Optional("dp"): int,
    vol.Optional("payloads"): list,
    vol.Optional("collect_seconds"): vol.Any(int, float),
})

CREATE_TEMP_PASSWORD_SCHEMA = vol.Schema({
    vol.Required("device_id"): str,
    vol.Required("name"): str,
    vol.Required("pin_code"): str,
    vol.Required("effective_time"): str,
    vol.Required("expiry_time"): str,
})


def _resolve_member_name(hass: HomeAssistant, call_data: dict) -> str:
    """Resolve person entity or member_name to a friendly name."""
    person_entity_id = call_data.get("person")
    if person_entity_id:
        state = hass.states.get(person_entity_id)
        if state:
            return state.name
        return person_entity_id.split(".")[-1].replace("_", " ").title()
    name = call_data.get("member_name")
    if name:
        return name
    return "Member"


def _get_coordinator(hass: HomeAssistant, device_id: str):
    """Resolve device_id to (mac, coordinator).

    Accepts MAC address, HA device registry ID, or config entry ID.
    """
    # Check all coordinators by MAC
    for entry in hass.config_entries.async_entries(DOMAIN):
        rd: TuyaBLELockData | None = getattr(entry, "runtime_data", None)
        if not rd or not rd.coordinators:
            continue
        # Direct MAC match
        mac_upper = device_id.upper()
        if mac_upper in rd.coordinators:
            return mac_upper, rd.coordinators[mac_upper]

    # Try device registry lookup
    from homeassistant.helpers import device_registry as dr
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device:
        for ident in device.identifiers:
            if ident[0] == DOMAIN:
                mac = ident[1]
                for entry in hass.config_entries.async_entries(DOMAIN):
                    rd = getattr(entry, "runtime_data", None)
                    if rd and mac in rd.coordinators:
                        return mac, rd.coordinators[mac]

    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="device_not_found",
        translation_placeholders={'device_id': device_id},
    )


def _get_service_dp(coordinator, service_name: str) -> int | None:
    profile = coordinator.profile or {}
    svc_cfg = profile.get("services", {}).get(service_name)
    if svc_cfg:
        return svc_cfg.get("dp")
    return None


def _get_sync_dp(coordinator, service_name: str) -> int | None:
    profile = coordinator.profile or {}
    svc_cfg = profile.get("services", {}).get(service_name)
    if svc_cfg:
        return svc_cfg.get("sync_dp")
    return None


def _refresh_credentials_ui(hass, mac: str | None = None) -> None:
    """Push a state refresh to the Credentials sensor(s) after a change.

    Credential edits live in the store, not the DataUpdateCoordinator, so nudge
    the coordinator's listeners so the sensor re-reads promptly. With a mac,
    only that lock's coordinator; otherwise all of them.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        rd = getattr(entry, "runtime_data", None)
        coordinators = getattr(rd, "coordinators", None) if rd else None
        if not coordinators:
            continue
        for cmac, coord in coordinators.items():
            if mac is None or cmac == mac:
                try:
                    coord.async_update_listeners()
                except Exception:  # pragma: no cover - defensive
                    pass


async def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "activate"):
        return


    async def handle_activate(call: ServiceCall) -> dict:
        entry = service_helper.async_get_config_entry(hass, DOMAIN, None)
        address = call.data["address"]
        try:
            record = await async_activate_lock(
                hass,
                entry,
                address=address,
                device_uuid=call.data.get("device_uuid", ""),
                name=call.data.get("name", ""),
            )
        except ActivationError as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="activation_failed",
                translation_placeholders={'details': str(exc)},
            ) from exc
        except Exception as exc:
            _LOGGER.error(
                "Unexpected error activating lock %s", address, exc_info=True
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="activation_unexpected",
            ) from exc

        return {
            "address": address,
            "name": record.get("name", ""),
            "uuid": record.get("uuid", ""),
        }

    async def handle_add_pin(call: ServiceCall) -> None:
        device_ids = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        pin_code = call.data["pin_code"]
        admin = call.data.get("admin", False)

        if not pin_code.isdigit() or len(pin_code) < 6:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_pin",
            )

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        person_eid = call.data.get("person")
        if not member:
            member = await store.async_add_member(member_name, person_entity_id=person_eid)
        elif person_eid and getattr(member, "person_entity_id", None) != person_eid:
            await store.async_update_member(member.member_id, person_entity_id=person_eid)
            member = store.get_member(member.member_id)

        if not isinstance(device_ids, list):
            device_ids = [device_ids]
        for device_id in device_ids:
            mac, coordinator = _get_coordinator(hass, device_id)
            dp_create = _get_service_dp(coordinator, "add_pin")
            if dp_create is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="service_unsupported",
                    translation_placeholders={'service': 'add_pin'},
                )
            try:
                await coordinator._async_ensure_connected()
                pin_bytes = [int(d) for d in pin_code]
                payload = build_enroll_payload(
                    CRED_PASSWORD, member.member_id, admin=admin, password_digits=pin_bytes
                )
                result = await coordinator._session.async_send_dp_raw(
                    dp_create, payload
                )
                if result:
                    resp = parse_enroll_response(result["raw"])
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_PASSWORD,
                            hw_id=resp.get("hw_id", 0),
                            name=f"{member_name} PIN",
                        )
                    else:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="pin_enrollment_failed",
                            translation_placeholders={'details': str(resp)},
                        )
                else:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="pin_no_response",
                    )
            finally:
                await coordinator._session.async_disconnect()

    async def handle_register_credential(call: ServiceCall) -> dict:
        """Associate a pre-existing lock credential slot with a HA member."""
        hw_id = call.data["hw_id"]
        if hw_id < 1 or hw_id > 0xFFFFFFFF:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_hardware_id",
            )

        mac, _coordinator = _get_coordinator(hass, call.data["device_id"])
        member_name = _resolve_member_name(hass, call.data)
        person_eid = call.data.get("person")
        cred_type_name = call.data["cred_type"]
        cred_type = CRED_TYPE_NAMES[cred_type_name]
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]

        # Upsert: re-registering a slot reassigns it (to a different person, or
        # with a new label) instead of erroring. A hardware slot holds exactly
        # one credential, so replacing the attribution is the correct semantics
        # and avoids forcing a delete-then-register dance.
        existing = store.find_credential(mac, cred_type, hw_id)

        member = store.get_member_by_name(member_name)
        if not member:
            member = await store.async_add_member(
                member_name, person_entity_id=person_eid
            )
        elif person_eid and getattr(member, "person_entity_id", None) != person_eid:
            await store.async_update_member(
                member.member_id, person_entity_id=person_eid
            )
            member = store.get_member(member.member_id)

        credential_name = (call.data.get("name") or "").strip()
        if not credential_name:
            credential_name = f"{member_name} {CRED_TYPE_LABELS[cred_type].title()}"
        credential = await store.async_add_credential(
            member_id=member.member_id,
            lock_entry_id=mac,
            cred_type=cred_type,
            hw_id=hw_id,
            name=credential_name,
        )
        _refresh_credentials_ui(hass, mac)
        return {
            "credential_id": credential.credential_id,
            "member_id": member.member_id,
            "member_name": member.name,
            "credential_type": cred_type_name,
            "hardware_id": hw_id,
            "name": credential.name,
            "replaced": existing.name if existing else None,
        }

    async def handle_add_fingerprint(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        admin = call.data.get("admin", False)
        finger = (call.data.get("finger") or "").strip() or None
        if finger in {
            f"{side}_{digit}" for side in ("right", "left")
            for digit in ("thumb", "index", "middle", "ring", "pinky")
        }:
            finger = finger.replace("_", " ").capitalize()

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        person_eid = call.data.get("person")
        if not member:
            member = await store.async_add_member(member_name, person_entity_id=person_eid)
        elif person_eid and getattr(member, "person_entity_id", None) != person_eid:
            await store.async_update_member(member.member_id, person_entity_id=person_eid)
            member = store.get_member(member.member_id)

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_create = _get_service_dp(coordinator, "add_fingerprint")
        if dp_create is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="service_unsupported",
                translation_placeholders={'service': 'add_fingerprint'},
            )
        sync_dp = _get_sync_dp(coordinator, "add_fingerprint")
        try:
            await coordinator._async_ensure_connected()

            if sync_dp:
                await coordinator._session.async_send_dp_raw(sync_dp, SYNC_MARKER)

            payload = build_enroll_payload(CRED_FINGERPRINT, member.member_id, admin=admin)
            results = await coordinator._session.async_send_dp_raw_long(
                dp_create, payload, timeout=60.0
            )

            for dp in results:
                if dp["id"] == dp_create and dp["type"] == 0 and len(dp["raw"]) >= 7:
                    resp = parse_enroll_response(dp["raw"])
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        cred_name = (
                            f"{member_name} {finger}" if finger
                            else f"{member_name} Fingerprint"
                        )
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_FINGERPRINT,
                            hw_id=resp.get("hw_id", 0),
                            name=cred_name,
                        )
                        _refresh_credentials_ui(hass, mac)
                        return
                    elif resp.get("stage") in ("FAILED", "CANCELLED"):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="fingerprint_enrollment_failed",
                            translation_placeholders={'details': str(resp)},
                        )

            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="fingerprint_timeout",
            )
        finally:
            await coordinator._session.async_disconnect()

    async def handle_add_card(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        member_name = _resolve_member_name(hass, call.data)
        admin = call.data.get("admin", False)

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        member = store.get_member_by_name(member_name)
        person_eid = call.data.get("person")
        if not member:
            member = await store.async_add_member(member_name, person_entity_id=person_eid)
        elif person_eid and getattr(member, "person_entity_id", None) != person_eid:
            await store.async_update_member(member.member_id, person_entity_id=person_eid)
            member = store.get_member(member.member_id)

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_create = _get_service_dp(coordinator, "add_card")
        if dp_create is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="service_unsupported",
                translation_placeholders={'service': 'add_card'},
            )
        sync_dp = _get_sync_dp(coordinator, "add_card")
        try:
            await coordinator._async_ensure_connected()

            if sync_dp:
                await coordinator._session.async_send_dp_raw(sync_dp, SYNC_MARKER)

            payload = build_enroll_payload(CRED_CARD, member.member_id, admin=admin)
            results = await coordinator._session.async_send_dp_raw_long(
                dp_create, payload, timeout=30.0
            )

            for dp in results:
                if dp["id"] == dp_create and dp["type"] == 0 and len(dp["raw"]) >= 7:
                    resp = parse_enroll_response(dp["raw"])
                    if resp.get("type") != "card":
                        continue
                    if resp.get("stage") == "COMPLETE" and resp.get("result") == "OK":
                        if resp["hw_id"] == 0xFF:
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="card_enrollment_unconfirmed",
                            )
                        await store.async_add_credential(
                            member_id=member.member_id,
                            lock_entry_id=mac,
                            cred_type=CRED_CARD,
                            hw_id=resp.get("hw_id", 0),
                            name=f"{member_name} Card",
                        )
                        _refresh_credentials_ui(hass, mac)
                        return
                    elif resp.get("stage") in ("FAILED", "CANCELLED"):
                        # Tuya UnlockModeListener: 0x08 means an already-bound
                        # card, not unsupported NFC. The 0xFF hardware ID in
                        # this response cannot identify an existing slot.
                        # https://developer.tuya.com/en/docs/app-development/bluetoothlock?id=Kceuheq7rzyf8
                        if resp["stage"] == "FAILED" and resp.get("result_code") == 0x08:
                            raise HomeAssistantError(
                                translation_domain=DOMAIN,
                                translation_key="card_already_enrolled",
                            )
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="card_enrollment_failed",
                            translation_placeholders={'details': str(resp)},
                        )

            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="card_timeout",
            )
        finally:
            await coordinator._session.async_disconnect()

    async def handle_delete_credential(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        credential_id = call.data.get("credential_id")

        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        mac, coordinator = _get_coordinator(hass, device_id)
        dp_delete = _get_service_dp(coordinator, "delete_credential")
        if dp_delete is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="service_unsupported",
                translation_placeholders={'service': 'delete_credential'},
            )

        if credential_id:
            cred_data = store._data["credentials"].get(credential_id)
            if not cred_data:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="credential_not_found",
                    translation_placeholders={'credential_id': credential_id},
                )
            creds_to_delete = [(credential_id, cred_data)]
        else:
            member_name = _resolve_member_name(hass, call.data)
            member = store.get_member_by_name(member_name)
            if not member:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="member_not_found",
                    translation_placeholders={'member_name': member_name},
                )

            cred_type_filter = None
            if call.data.get("cred_type"):
                cred_type_filter = CRED_TYPE_NAMES[call.data["cred_type"]]

            creds_to_delete = [
                (cid, c) for cid, c in store._data["credentials"].items()
                if c["member_id"] == member.member_id
                and c["lock_entry_id"] == mac
                and (cred_type_filter is None or c["cred_type"] == cred_type_filter)
            ]
            if not creds_to_delete:
                ctype = call.data.get("cred_type", "any")
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="no_credentials",
                    translation_placeholders={'cred_type': ctype, 'member_name': member_name},
                )

        try:
            await coordinator._async_ensure_connected()

            for cid, cred_data in creds_to_delete:
                delete_payload = build_delete_payload(
                    cred_type=cred_data["cred_type"],
                    member_id=cred_data["member_id"],
                    hw_id=cred_data["hw_id"],
                )
                result = await coordinator._session.async_send_dp_raw(
                    dp_delete, delete_payload
                )
                _LOGGER.info("Delete credential %s result: %s", cred_data.get("name", cid), result)
                await store.async_delete_credential(cid)
            _refresh_credentials_ui(hass, mac)
        finally:
            await coordinator._session.async_disconnect()

    async def handle_create_temp_password(call: ServiceCall) -> dict:
        device_id = call.data["device_id"]
        name = call.data["name"]
        pin_code = call.data["pin_code"]
        effective_time = call.data["effective_time"]
        expiry_time = call.data["expiry_time"]

        try:
            eff_ts, exp_ts = parse_credential_window(
                effective_time, expiry_time, hass.config.time_zone
            )
        except CredentialTimeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
            ) from err

        if not re.fullmatch(r"[0-9]{6,255}", pin_code):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_pin",
            )

        mac, coordinator = _get_coordinator(hass, device_id)
        dp_temp = _get_service_dp(coordinator, "create_temp_password")
        if dp_temp is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="service_unsupported",
                translation_placeholders={'service': 'create_temp_password'},
            )
        async with coordinator._op_lock:
            await coordinator._async_ensure_connected()
            async with coordinator._temp_password_lock:
                try:
                    store: CredentialStore = hass.data[DOMAIN]["credential_store"]

                    pin_bytes = [int(d) for d in pin_code]
                    payload = build_temp_password_payload(pin_bytes, name, eff_ts, exp_ts)
                    result = await coordinator._session.async_send_dp_raw(
                        dp_temp, payload
                    )
                    if result is None:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="temp_password_no_response",
                        )
                    try:
                        if result.get("id") != dp_temp or result.get("type") != 0:
                            raise ValueError("Unexpected datapoint")
                        response = parse_temp_password_response(result.get("raw", b""))
                    except (ValueError, TypeError):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="temp_password_unconfirmed",
                        ) from None
                    if response["result_code"] != 0:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="temp_password_rejected",
                            translation_placeholders={"code": f'0x{response["result_code"]:02X}'},
                        )
                    if response["hw_id"] == 255:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="temp_password_unconfirmed",
                        )
                    rec = await store.async_add_temp_password(
                        lock_entry_id=mac, name=name, effective=eff_ts, expiry=exp_ts,
                        hw_id=response["hw_id"],
                    )
                    _refresh_credentials_ui(hass, mac)
                    return {"password_id": rec.password_id, "name": rec.name,
                            "hw_id": rec.hw_id, "effective_ts": eff_ts, "expiry_ts": exp_ts}
                finally:
                    await coordinator._session.async_disconnect()

    async def handle_list_credentials(call: ServiceCall):
        device_id = call.data["device_id"]
        mac, coordinator = _get_coordinator(hass, device_id)
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]

        creds = store.get_credentials_for_lock(mac)
        members = {m.member_id: m.name for m in store.get_members()}

        result = []
        for c in creds:
            result.append({
                "credential_id": c.credential_id,
                "member": members.get(c.member_id, f"member_{c.member_id}"),
                "type": CRED_TYPE_LABELS.get(c.cred_type, f"unknown_{c.cred_type}"),
                "name": c.name,
                "hw_id": c.hw_id,
            })
        return {"credentials": result, **store.get_temp_password_overview(mac)}

    service_helper.async_register_admin_service(
        hass,
        DOMAIN,
        "activate",
        handle_activate,
        schema=ACTIVATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    service_helper.async_register_admin_service(
        hass,
        DOMAIN,
        "register_credential",
        handle_register_credential,
        schema=REGISTER_CREDENTIAL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async def handle_probe_records(call: ServiceCall) -> dict:
        """Sweep candidate history-read requests and report everything returned.

        Diagnostic only, and neither sweep can change lock state. The first
        writes the "Get Records" DP (69 by default), which the integration
        already writes on every session, varying the index/count field and the
        trailing selector byte. The second reads CMD_DEVICE_STATUS with an
        explicit DP-id list instead of the empty payload we normally send, on
        the theory that the record DPs are there but have to be asked for.
        """
        mac, coordinator = _get_coordinator(hass, call.data["device_id"])
        dp_id = call.data.get("dp", 69)
        collect = call.data.get("collect_seconds", 6.0)

        code_str = getattr(coordinator._session, "_check_code", "") or ""
        code = (code_str.encode("ascii") + b"\x00" * 8)[:8]

        if call.data.get("payloads"):
            dp_variants = [
                (hex_str, bytes.fromhex(hex_str))
                for hex_str in call.data["payloads"]
            ]
        else:
            # index:2, count:2, check_code:8, selector:1
            dp_variants = [
                (label, prefix + code + suffix)
                for label, prefix, suffix in (
                    ("baseline ffff/0001 sel=00", b"\xff\xff\x00\x01", b"\x00"),
                    ("index 0000/0001 sel=00", b"\x00\x00\x00\x01", b"\x00"),
                    ("index 0000/000a sel=00", b"\x00\x00\x00\x0a", b"\x00"),
                    ("newest ffff/000a sel=00", b"\xff\xff\x00\x0a", b"\x00"),
                    ("baseline sel=01", b"\xff\xff\x00\x01", b"\x01"),
                    ("baseline sel=02", b"\xff\xff\x00\x01", b"\x02"),
                )
            ]

        # The record DPs, asked for by id. DP20 is the lock's own record
        # datapoint; 12/13/15/19 are the per-method unlock records we so far
        # only ever see pushed live on 0x8007.
        status_variants = [
            ("status DP20", b"\x14"),
            ("status DP69", b"\x45"),
            ("status DP72", b"\x48"),
            ("status DP12/13/15/19", b"\x0c\x0d\x0f\x13"),
            ("status all records", b"\x0c\x0d\x0f\x13\x14\x45\x48"),
        ]

        results: dict[str, dict] = {}

        def _record(label: str, out: dict) -> None:
            frames = [f"0x{f['cmd']:04x}:{f['hex']}" for f in out["frames"]]
            dps = [f"DP{d['id']}={d['raw'].hex()}" for d in out["dps"]]
            _LOGGER.info(
                "PROBE %s <- frames=%s dps=%s", label, frames or "-", dps or "-"
            )
            results[label] = {"frames": frames, "dps": dps}

        # The lock drops the link partway through a long sweep, so reconnect
        # per variant rather than assuming one session survives all of them.
        # Status reads run first: they are the ones carrying new information.
        async def _run(label: str, send) -> None:
            try:
                await coordinator._async_ensure_connected()
                out = await send()
            except Exception as exc:
                _LOGGER.warning("PROBE %s failed: %s", label, exc)
                results[label] = {"error": str(exc)}
                return
            _record(label, out)

        try:
            for label, payload in status_variants:
                _LOGGER.info("PROBE %s -> 0x0003 %s", label, payload.hex())
                await _run(
                    label,
                    lambda p=payload: coordinator._session.async_probe_status(
                        p, collect_seconds=collect
                    ),
                )
            for label, payload in dp_variants:
                _LOGGER.info("PROBE %s -> DP%d %s", label, dp_id, payload.hex())
                await _run(
                    label,
                    lambda p=payload: coordinator._session.async_probe_raw(
                        dp_id, p, collect_seconds=collect
                    ),
                )
        finally:
            await coordinator._session.async_disconnect()

        return {"mac": mac, "dp": dp_id, "results": results}

    hass.services.async_register(DOMAIN, "add_pin", handle_add_pin, schema=ADD_PIN_SCHEMA)
    hass.services.async_register(DOMAIN, "add_fingerprint", handle_add_fingerprint, schema=ADD_FINGERPRINT_SCHEMA)
    hass.services.async_register(DOMAIN, "add_card", handle_add_card, schema=ADD_CARD_SCHEMA)
    async def handle_clear_credentials(call: ServiceCall) -> dict:
        """Empty the HA credential/attribution store.

        With a device selected, clears only that lock's credentials; without
        one, wipes every credential and member. Metadata only -- it does NOT
        remove fingerprints from the lock hardware (use delete_credential or
        the lock's own menu for that).
        """
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        device_id = call.data.get("device_id")
        mac = None
        if device_id:
            mac, _ = _get_coordinator(hass, device_id)
        removed = await store.async_clear(lock_entry_id=mac)
        _LOGGER.info(
            "Cleared %d credential(s) from store (scope=%s)",
            removed, mac or "all",
        )
        _refresh_credentials_ui(hass, mac)
        return {"removed": removed, "scope": mac or "all"}

    async def handle_report_factory_reset(call: ServiceCall) -> dict:
        """Record a hardware reset by clearing only this lock's local records."""
        mac, _ = _get_coordinator(hass, call.data["device_id"])
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        removed = await store.async_report_factory_reset(mac)
        _refresh_credentials_ui(hass, mac)
        _LOGGER.info(
            "Factory reset reported for %s: removed %d credentials and %d temporary passwords",
            mac, removed["credentials"], removed["temp_passwords"],
        )
        return {"mac": mac, "removed": removed}

    async def handle_sync_credentials(call: ServiceCall) -> dict:
        """Read the lock's actual credential slots via DP54 (read-only).

        Queries each credential type and returns the occupied hardware slots the
        LOCK reports, cross-referenced with the HA store. Slots the store does
        not know about are flagged known=false -- those are the orphans left by
        re-pairs or enrollments done outside HA. Nothing is written or deleted.
        """
        device_id = call.data["device_id"]
        mac, coordinator = _get_coordinator(hass, device_id)
        dp_sync = _get_service_dp(coordinator, "sync_credentials")
        if dp_sync is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="service_unsupported",
                translation_placeholders={'service': 'sync_credentials'},
            )
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        members = {m.member_id: m.name for m in store.get_members()}
        types = {
            "fingerprint": CRED_FINGERPRINT,
            "pin": CRED_PASSWORD,
            "card": CRED_CARD,
        }
        result: dict[str, list] = {}
        errors: dict[str, str] = {}
        try:
            # The lock drops the link between queries, so reconnect per type and
            # keep going if one type fails instead of losing the whole sync.
            for name, ctype in types.items():
                try:
                    await coordinator._async_ensure_connected()
                    dps = await coordinator._session.async_send_dp_raw_long(
                        dp_sync, bytes([ctype]), timeout=8.0
                    )
                except Exception as exc:
                    _LOGGER.warning("Sync %s on %s failed: %s", name, mac, exc)
                    errors[name] = str(exc)
                    continue
                # The lock answers with a record list plus a short summary
                # frame; accumulate over every DP54 frame instead of keeping
                # only the last, which would drop the actual list.
                records: list[dict] = []
                for d in dps:
                    if d["id"] == dp_sync and d["type"] == 0:
                        records.extend(parse_credential_list(d["raw"]))
                entries = []
                seen: set[int] = set()
                for rec in records:
                    if rec["cred_type"] != ctype or rec["hw_id"] in seen:
                        continue
                    seen.add(rec["hw_id"])
                    cred = store.find_credential(mac, ctype, rec["hw_id"])
                    entries.append({
                        "hw_id": rec["hw_id"],
                        "known": cred is not None,
                        "name": cred.name if cred else None,
                        "member": members.get(cred.member_id) if cred else None,
                        "lock_member_id": rec["member_id"],
                    })
                result[name] = entries
                _LOGGER.info(
                    "Sync %s on %s: %d slot(s) %s",
                    name, mac, len(entries), [e["hw_id"] for e in entries],
                )
        finally:
            await coordinator._session.async_disconnect()
        if not result and errors:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_failed",
                translation_placeholders={'details': "; ".join(f"{k}: {v}" for k, v in errors.items())},
            )
        coordinator.last_credential_sync = result
        _refresh_credentials_ui(hass, mac)
        out = {"mac": mac, "credentials": result}
        if errors:
            out["errors"] = errors
        return out

    async def _async_read_fingerprint_records(
        coordinator, dp_sync
    ) -> tuple[list[dict], bool]:
        """Ask the lock which fingerprint slots it holds (reconnects itself).

        Returns (records, answered). `answered` is False when the lock sent no
        DP54 frame at all -- that must never be mistaken for "no fingerprints",
        or a clear would wipe attribution on the strength of silence.
        """
        await coordinator._async_ensure_connected()
        dps = await coordinator._session.async_send_dp_raw_long(
            dp_sync, bytes([CRED_FINGERPRINT]), timeout=8.0
        )
        records: list[dict] = []
        seen: set[int] = set()
        answered = False
        for d in dps:
            if d["id"] != dp_sync or d["type"] != 0:
                continue
            answered = True
            for rec in parse_credential_list(d["raw"]):
                if rec["cred_type"] == CRED_FINGERPRINT and rec["hw_id"] not in seen:
                    seen.add(rec["hw_id"])
                    records.append(rec)
        return records, answered

    async def handle_clear_fingerprints(call: ServiceCall) -> dict:
        """Delete fingerprints from the lock itself, then clear HA attribution.

        Enumerates the lock's actual fingerprint slots (DP54) rather than
        trusting the HA store, so orphans left by re-pairs are removed too, and
        uses the member id the LOCK reports for each slot instead of guessing.
        Re-reads afterwards so the response proves what is really gone.
        Pass hw_id to remove a single slot -- use that to verify first.
        """
        device_id = call.data["device_id"]
        only = call.data.get("hw_id")
        mac, coordinator = _get_coordinator(hass, device_id)
        dp_sync = _get_service_dp(coordinator, "sync_credentials")
        dp_delete = _get_service_dp(coordinator, "delete_credential")
        if dp_sync is None or dp_delete is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="clear_unsupported",
            )
        store: CredentialStore = hass.data[DOMAIN]["credential_store"]
        try:
            before, answered = await _async_read_fingerprint_records(
                coordinator, dp_sync
            )
            if not answered:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="query_no_response",
                )
            targets = [r for r in before if only is None or r["hw_id"] == only]
            if only is not None and not targets:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="fingerprint_not_found",
                    translation_placeholders={'slot': str(only)},
                )
            attempted, failed = [], {}
            for rec in targets:
                payload = build_delete_payload(
                    cred_type=CRED_FINGERPRINT,
                    member_id=rec["member_id"],
                    hw_id=rec["hw_id"],
                )
                try:
                    await coordinator._async_ensure_connected()
                    resp = await coordinator._session.async_send_dp_raw(
                        dp_delete, payload
                    )
                except Exception as exc:
                    _LOGGER.warning(
                        "Deleting fingerprint slot %s failed: %s", rec["hw_id"], exc
                    )
                    failed[rec["hw_id"]] = str(exc)
                    continue
                # The lock echoes the request plus a trailing stage byte
                # (0xFF = done). Rather than trust an undocumented status code,
                # success is decided by the verification read below: a slot is
                # deleted only if it is genuinely gone afterwards.
                _LOGGER.debug(
                    "Delete slot %s -> %s",
                    rec["hw_id"], (resp or {}).get("raw", b"").hex() or "(no reply)",
                )
                attempted.append(rec["hw_id"])
            after, after_answered = await _async_read_fingerprint_records(
                coordinator, dp_sync
            )
        finally:
            await coordinator._session.async_disconnect()

        if not after_answered:
            # Silence is not proof of deletion — leave attribution untouched.
            _LOGGER.warning(
                "Verification read after delete got no answer from %s", mac
            )
            return {
                "mac": mac,
                "before": [r["hw_id"] for r in before],
                "attempted": attempted,
                "remaining_on_lock": "unknown (lock did not answer)",
                "failed": failed,
            }

        remaining = {r["hw_id"] for r in after}
        deleted = [hw for hw in attempted if hw not in remaining]
        for hw in attempted:
            if hw in remaining:
                failed[hw] = "still present after delete"
        for hw_id in deleted:
            cred = store.find_credential(mac, CRED_FINGERPRINT, hw_id)
            if cred:
                await store.async_delete_credential(cred.credential_id)
        _refresh_credentials_ui(hass, mac)
        _LOGGER.info(
            "Clear fingerprints on %s: before=%s deleted=%s remaining=%s",
            mac, [r["hw_id"] for r in before], deleted, sorted(remaining),
        )
        return {
            "mac": mac,
            "before": [r["hw_id"] for r in before],
            "deleted": deleted,
            "remaining_on_lock": sorted(remaining),
            "failed": failed,
        }

    hass.services.async_register(DOMAIN, "delete_credential", handle_delete_credential, schema=DELETE_CREDENTIAL_SCHEMA)
    hass.services.async_register(DOMAIN, "list_credentials", handle_list_credentials, schema=LIST_CREDENTIALS_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "clear_credentials", handle_clear_credentials, schema=CLEAR_CREDENTIALS_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    service_helper.async_register_admin_service(
        hass, DOMAIN, "report_factory_reset", handle_report_factory_reset,
        schema=REPORT_FACTORY_RESET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "sync_credentials", handle_sync_credentials, schema=SYNC_CREDENTIALS_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "clear_fingerprints", handle_clear_fingerprints, schema=CLEAR_FINGERPRINTS_SCHEMA, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "create_temp_password", handle_create_temp_password, schema=CREATE_TEMP_PASSWORD_SCHEMA, supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "probe_records", handle_probe_records, schema=PROBE_RECORDS_SCHEMA, supports_response=SupportsResponse.ONLY)
