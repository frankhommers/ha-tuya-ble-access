"""Shared orchestration for first activation of Tuya V5 BLE locks."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .ble_session import (
    BindVerificationError,
    DeviceAlreadyBoundError,
    PairingFailedError,
    TuyaBLELockSession,
)
from .const import (
    CONF_TUYA_COUNTRY,
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_REGION,
    DOMAIN,
)
from .device_store import DeviceStore
from .tuya_cloud import async_fetch_auth_key

_LOGGER = logging.getLogger(__name__)


class ActivationError(Exception):
    """Base error for lock activation orchestration."""


class MissingActivationSeedError(ActivationError):
    """Raised when the cloud response lacks a usable V5 activation seed."""


class BluetoothUnavailableError(ActivationError):
    """Raised when Home Assistant cannot resolve a connectable BLE device."""


class DeviceAlreadyBoundActivationError(ActivationError):
    """Raised when first activation is attempted on an already-bound lock."""


class PairingFailedActivationError(ActivationError):
    """Raised when the lock does not accept or complete PAIR."""


class BindVerificationActivationError(ActivationError):
    """Raised when PAIR succeeds but the bound state cannot be verified."""


class CloudFetchActivationError(ActivationError):
    """Raised when activation data cannot be fetched from Tuya cloud."""


class StorageUnavailableActivationError(ActivationError):
    """Raised when storage cannot be checked before physical binding."""


class PostBindPersistenceActivationError(ActivationError):
    """Raised when credentials cannot be persisted or loaded after binding."""


def _decode_hex(name: str, value: object, length: int) -> bytes:
    if not isinstance(value, str):
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be a string"
        )
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be hex"
        ) from exc
    if len(decoded) != length:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be {length}-byte hex"
        )
    return decoded


def _encode_ascii_key(name: str, value: object) -> bytes:
    if not isinstance(value, str):
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be an ASCII string"
        )
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must encode as exactly 16 ASCII bytes"
        ) from exc
    if len(encoded) != 16:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must encode as exactly 16 ASCII bytes"
        )
    return encoded


def _validate_ascii_identifier(name: str, value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be a non-empty ASCII string"
        )
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MissingActivationSeedError(
            f"Invalid V5 activation seed: {name} must be a non-empty ASCII string"
        ) from exc


def validate_activation_seed(cloud: dict, device_uuid: str = "") -> dict:
    """Normalize and validate credentials before offering physical activation."""
    device_id = cloud.get("device_id") or cloud.get("devId") or ""
    if not device_id and cloud.get("virtual_id"):
        try:
            device_id = bytes.fromhex(cloud["virtual_id"]).rstrip(b"\x00").decode("ascii")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise MissingActivationSeedError("Invalid stored device identifier") from exc
    seed = {
        **cloud,
        "auth_key": cloud.get("auth_key") or cloud.get("encryptedAuthKey") or "",
        "auth_random": cloud.get("auth_random") or cloud.get("random") or "",
        "local_key": cloud.get("local_key") or cloud.get("localKey") or "",
        "sec_key": cloud.get("sec_key") or cloud.get("secKey") or "",
        "verify_key": cloud.get("verify_key") or cloud.get("verifyKey") or cloud.get("sign") or "",
        "device_id": device_id,
        "uuid": cloud.get("uuid") or device_uuid,
    }
    _decode_hex("auth_key", seed["auth_key"], 16)
    _decode_hex("auth_random", seed["auth_random"], 16)
    _encode_ascii_key("local_key", seed["local_key"])
    _encode_ascii_key("sec_key", seed["sec_key"])
    _decode_hex("verify_key", seed["verify_key"], 4)
    _validate_ascii_identifier("device_id", seed["device_id"])
    _validate_ascii_identifier("uuid", seed["uuid"])
    return seed


async def _async_check_storage(hass: HomeAssistant, persistence_lock: asyncio.Lock) -> None:
    try:
        async with persistence_lock:
            device_store = DeviceStore(hass)
            await device_store.async_load()
    except Exception as exc:
        raise StorageUnavailableActivationError(
            "Device storage is unavailable; activation was not started"
        ) from exc


async def _async_persist_record(
    hass: HomeAssistant,
    persistence_lock: asyncio.Lock,
    address: str,
    record: dict,
) -> None:
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            async with persistence_lock:
                device_store = DeviceStore(hass)
                await device_store.async_load()
                await device_store.async_add_device(address, record)
            return
        except Exception as exc:
            last_error = exc
    raise PostBindPersistenceActivationError(
        "Lock activation succeeded but credentials could not be persisted after 3 attempts"
    ) from last_error


async def _async_reload_entry(hass: HomeAssistant, entry) -> None:
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except Exception as exc:
        raise PostBindPersistenceActivationError(
            "Lock activation succeeded but the integration could not be reloaded"
        ) from exc


async def _async_complete_post_bind(
    hass: HomeAssistant,
    entry,
    session: TuyaBLELockSession,
    persistence_lock: asyncio.Lock,
    address: str,
    record: dict,
) -> Exception | None:
    try:
        await session.async_disconnect()
    except Exception:
        _LOGGER.warning(
            "Failed to disconnect %s after activation",
            address,
            exc_info=True,
        )
    try:
        await _async_persist_record(hass, persistence_lock, address, record)
        await _async_reload_entry(hass, entry)
    except Exception as exc:
        return exc
    return None


async def _async_finish_cancelled_completion(
    completion_task: asyncio.Task, address: str
) -> None:
    while not completion_task.done():
        try:
            await asyncio.shield(completion_task)
        except asyncio.CancelledError:
            continue
    completion_error = completion_task.result()
    if completion_error is not None:
        _LOGGER.error(
            "Post-bind persistence failed for %s during cancellation",
            address,
            exc_info=(
                type(completion_error),
                completion_error,
                completion_error.__traceback__,
            ),
        )


async def async_activate_lock(
    hass: HomeAssistant,
    entry,
    *,
    address: str,
    device_uuid: str = "",
    name: str = "",
    activation_seed: dict | None = None,
) -> dict:
    """Fetch a V5 seed, verify BLE activation, then persist the lock."""
    normalized_address = address.upper()
    domain_data = hass.data.setdefault(DOMAIN, {})
    locks = domain_data.setdefault("activation_locks", {})
    lock = locks.setdefault(normalized_address, asyncio.Lock())
    persistence_lock = domain_data.setdefault(
        "activation_persistence_lock", asyncio.Lock()
    )

    async with lock, AsyncExitStack() as connections:
        if activation_seed is None:
            try:
                async with asyncio.timeout(30):
                    activation_seed = await async_fetch_auth_key(
                        hass,
                        device_uuid,
                        entry.data.get(CONF_TUYA_EMAIL, ""),
                        entry.data.get(CONF_TUYA_PASSWORD, ""),
                        entry.data.get(CONF_TUYA_COUNTRY, ""),
                        entry.data.get(CONF_TUYA_REGION, ""),
                        device_mac=normalized_address,
                    )
            except Exception as exc:
                raise CloudFetchActivationError(
                    "Could not fetch lock activation data"
                ) from exc

        cloud = validate_activation_seed(activation_seed, device_uuid)
        auth_key_hex = cloud["auth_key"]
        auth_random_hex = cloud["auth_random"]
        local_key = cloud["local_key"]
        sec_key = cloud["sec_key"]
        verify_key_hex = cloud["verify_key"]
        device_id = cloud["device_id"]
        resolved_uuid = cloud["uuid"]
        auth_key = bytes.fromhex(auth_key_hex)
        auth_random = bytes.fromhex(auth_random_hex)
        verify_key = bytes.fromhex(verify_key_hex)
        local_key_bytes = local_key.encode("ascii")
        sec_key_bytes = sec_key.encode("ascii")
        device_id_bytes = device_id.encode("ascii")
        login_key = local_key_bytes[:6]
        virtual_id = (device_id_bytes + b"\x00" * 22)[:22]

        await _async_check_storage(hass, persistence_lock)

        ble_device = bluetooth.async_ble_device_from_address(
            hass, normalized_address, connectable=True
        )
        if ble_device is None:
            raise BluetoothUnavailableError(
                f"No connectable BLE device available for {normalized_address}"
            )

        runtime_data = getattr(entry, "runtime_data", None)
        coordinators = getattr(runtime_data, "coordinators", {})
        coordinator = coordinators.get(normalized_address)
        if coordinator is not None:
            # Suspend this lock's normal operations until pairing and reload finish.
            await connections.enter_async_context(coordinator._op_lock)
            if coordinator._idle_timer is not None:
                coordinator._idle_timer.cancel()
                coordinator._idle_timer = None
            await coordinator._session.async_disconnect()

        session = TuyaBLELockSession(
            hass,
            ble_device,
            login_key,
            virtual_id,
            resolved_uuid,
            auth_key=auth_key,
            auth_random=auth_random,
            local_key=local_key_bytes,
            sec_key=sec_key_bytes,
            verify_key=verify_key,
            check_code=cloud.get("check_code") or cloud.get("checkCode") or "",
        )
        try:
            login_key, virtual_id = await session.async_pair_first_activation(
                auth_key_hex
            )
        except BaseException as exc:
            try:
                await session.async_disconnect()
            except BaseException:
                _LOGGER.warning(
                    "Failed to disconnect %s after activation",
                    normalized_address,
                    exc_info=True,
                )
            if isinstance(exc, DeviceAlreadyBoundError):
                raise DeviceAlreadyBoundActivationError(
                    "Lock is already bound"
                ) from exc
            if isinstance(exc, PairingFailedError):
                raise PairingFailedActivationError("PAIR did not complete") from exc
            if isinstance(exc, BindVerificationError):
                raise BindVerificationActivationError(
                    "Lock binding could not be verified"
                ) from exc
            raise

        record = {
            "uuid": resolved_uuid,
            "login_key": login_key.hex(),
            "virtual_id": virtual_id.hex(),
            "auth_key": auth_key_hex,
            "auth_random": auth_random_hex,
            "product_id": cloud.get("product_id") or cloud.get("productId") or "",
            "name": name or cloud.get("name") or normalized_address,
            "local_key": local_key,
            "sec_key": sec_key,
            "verify_key": verify_key_hex,
            "check_code": cloud.get("check_code") or cloud.get("checkCode") or "",
            "cloud_dps": cloud.get("dps") or cloud.get("cloud_dps") or {},
        }
        completion_task = asyncio.create_task(
            _async_complete_post_bind(
                hass,
                entry,
                session,
                persistence_lock,
                normalized_address,
                record,
            )
        )
        try:
            completion_error = await asyncio.shield(completion_task)
        except asyncio.CancelledError:
            await _async_finish_cancelled_completion(
                completion_task, normalized_address
            )
            raise
        if completion_error is not None:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                await _async_finish_cancelled_completion(
                    completion_task, normalized_address
                )
                raise
            raise completion_error
        return record
