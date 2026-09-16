"""Diagnostics support for the Tuya BLE Access hub.

Dumps cloud config, per-device BLE credentials and current DP state in a
single JSON file when the user clicks "Download diagnostics" on the
integration. Secrets are redacted by default — HA's diagnostics helper
replaces them with `**REDACTED**`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_TUYA_EMAIL,
    CONF_TUYA_PASSWORD,
    CONF_TUYA_COUNTRY,
    CONF_TUYA_REGION,
)
from .models import TuyaBLELockData

_ENTRY_REDACT = {CONF_TUYA_EMAIL, CONF_TUYA_PASSWORD}
_DEVICE_REDACT = {
    "login_key",
    "virtual_id",
    "auth_key",
    "auth_random",
    "local_key",
    "sec_key",
    "verify_key",
    "check_code",
    "uuid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data: TuyaBLELockData | None = getattr(entry, "runtime_data", None)
    devices: dict[str, dict] = {}
    state_snapshot: dict[str, dict] = {}
    if data is not None:
        for mac, dev in data.device_store.devices.items():
            devices[mac] = async_redact_data(dev, _DEVICE_REDACT)
        for mac, coord in data.coordinators.items():
            state_snapshot[mac] = {
                "device_name": coord.device_name,
                "product_id": coord.device_data.get("product_id"),
                "is_connected": coord._session.is_connected,
                "persistent_connection": coord.persistent_connection,
                "profile_name": (coord.profile or {}).get("name"),
                "state": dict(coord.state),
            }
    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), _ENTRY_REDACT),
            "options": dict(entry.options or {}),
        },
        "devices": devices,
        "coordinators": state_snapshot,
    }
