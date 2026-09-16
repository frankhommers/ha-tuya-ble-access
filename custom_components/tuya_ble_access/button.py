"""Button platform for Tuya BLE lock."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_SETUP_METHOD, DOMAIN, SETUP_METHOD_CLOUD
from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        entities.append(TuyaBLERefreshStatusButton(coordinator, entry))
        entities.append(TuyaBLEReportFactoryResetButton(coordinator, entry))
        if entry.data.get(CONF_SETUP_METHOD, SETUP_METHOD_CLOUD) == SETUP_METHOD_CLOUD:
            entities.append(TuyaBLECloudRefreshButton(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaBLERefreshStatusButton(TuyaBLELockEntity, ButtonEntity):
    """Pull the lock's current state over BLE (battery, motor, settings)."""

    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class TuyaBLEReportFactoryResetButton(TuyaBLELockEntity, ButtonEntity):
    """Forget local credential mappings after a physical factory reset."""

    _attr_translation_key = "report_factory_reset"
    _attr_icon = "mdi:backup-restore"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_report_factory_reset"

    @property
    def available(self) -> bool:
        # A physical reset can invalidate the BLE connection.
        return True

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN, "report_factory_reset", {"device_id": self._mac},
            blocking=True, context=self._context,
        )


class TuyaBLECloudRefreshButton(TuyaBLELockEntity, ButtonEntity):
    """Re-sync keys and DP snapshot from the Tuya cloud for this lock.

    Use when the lock is out of BLE range, after a re-pair in the Tuya app
    (which rotates local_key/sec_key/check_code), or to populate
    event-only sensors (doorbell, hijack, last unlock) from cloud state.
    """

    _attr_translation_key = "cloud_refresh"
    _attr_icon = "mdi:cloud-refresh"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_cloud_refresh"

    async def async_press(self) -> None:
        from .tuya_cloud import async_refresh_one_device
        try:
            await async_refresh_one_device(
                self.hass, self._entry, self._mac,
            )
        except RuntimeError as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cloud_refresh_failed",
                translation_placeholders={'details': str(exc)},
            ) from exc
        # Pick up fresh credentials + seeded cloud DPs
        await self.hass.config_entries.async_reload(self._entry.entry_id)
