"""Base entity class for Tuya BLE lock devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class TuyaBLELockEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._entry = entry
        self._mac = coordinator.mac

    @property
    def device_info(self) -> DeviceInfo:
        model = "BLE Smart Lock"
        profile = self.coordinator.profile
        if profile:
            model = profile.get("model", model)
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name=self.coordinator.device_name,
            manufacturer="Tuya",
            model=model,
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            via_device=(DOMAIN, self._entry.entry_id),
        )

    @property
    def available(self) -> bool:
        return super().available
