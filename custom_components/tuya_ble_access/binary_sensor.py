"""Binary sensor platform for Tuya BLE lock."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData


# (state_key, translation key, unique_id suffix, device_class or None, icon)
_BINARY_SPECS: list[tuple[str, str, str, BinarySensorDeviceClass | None, str | None]] = [
    ("doorbell", "doorbell", "doorbell", None, "mdi:doorbell"),
    ("hijack", "hijack", "hijack", BinarySensorDeviceClass.SAFETY, "mdi:alert"),
    ("message", "message", "message", None, "mdi:message-alert"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        profile = coordinator.profile or {}
        state_map = profile.get("state_map", {})
        known_keys = {m.get("key") for m in state_map.values()}
        for key, translation_key, uid, dev_class, icon in _BINARY_SPECS:
            if key in known_keys:
                entities.append(
                    TuyaBLEBooleanSensor(coordinator, entry, key, translation_key, uid, dev_class, icon)
                )
    if entities:
        async_add_entities(entities)


class TuyaBLEBooleanSensor(TuyaBLELockEntity, BinarySensorEntity, RestoreEntity):
    """Generic binary sensor that reads a bool coordinator state key."""

    def __init__(self, coordinator, entry, state_key: str, translation_key: str, uid_suffix: str,
                 dev_class: BinarySensorDeviceClass | None, icon: str | None):
        self._attr_translation_key = translation_key
        self._state_key = state_key
        self._uid_suffix = uid_suffix
        if dev_class is not None:
            self._attr_device_class = dev_class
        if icon:
            self._attr_icon = icon
        super().__init__(coordinator, entry)

    @property
    def unique_id(self) -> str:
        return f"{self._mac}_{self._uid_suffix}"

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.state.get(self._state_key)
        if val is None:
            return None
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.lower() in ("true", "1", "on", "yes")
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get(self._state_key) is None:
            last = await self.async_get_last_state()
            if last and last.state in ("on", "off"):
                self.coordinator.state[self._state_key] = last.state == "on"
