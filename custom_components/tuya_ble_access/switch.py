"""Switch platform for Tuya BLE lock."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        profile = coordinator.profile or {}
        entities_cfg = profile.get("entities", {})
        entities.append(TuyaBLEPersistentConnectionSwitch(coordinator, entry))
        if "double_lock_switch" in entities_cfg:
            entities.append(TuyaBLEDoubleLockSwitch(coordinator, entry))
        if "passage_mode_switch" in entities_cfg:
            entities.append(TuyaBLEPassageModeSwitch(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaBLEDoubleLockSwitch(TuyaBLELockEntity, SwitchEntity, RestoreEntity):
    _attr_translation_key = "double_lock"
    _attr_assumed_state = False

    @property
    def unique_id(self):
        return f"{self._mac}_double_lock"

    @property
    def icon(self) -> str:
        if self.is_on:
            return "mdi:lock"
        return "mdi:lock-open"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.get("double_lock")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("double_lock") is None:
            last = await self.async_get_last_state()
            if last and last.state in ("on", "off"):
                self.coordinator.state["double_lock"] = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_double_lock(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_double_lock(False)


class TuyaBLEPassageModeSwitch(TuyaBLELockEntity, SwitchEntity, RestoreEntity):
    """Passage mode: lock stays open until manually locked.

    Inverted from the underlying DP 33 (auto_lock):
      Passage ON  = auto_lock OFF = lock stays unlocked
      Passage OFF = auto_lock ON  = lock auto-locks normally
    """

    _attr_translation_key = "passage_mode"
    _attr_assumed_state = False

    @property
    def unique_id(self):
        return f"{self._mac}_passage_mode"

    @property
    def icon(self) -> str:
        return "mdi:door-open" if self.is_on else "mdi:lock-clock"

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.state.get("auto_lock")
        if val is None:
            return None
        return not val  # inverted: auto_lock=False -> passage=ON

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("auto_lock") is None:
            last = await self.async_get_last_state()
            if last and last.state in ("on", "off"):
                self.coordinator.state["auto_lock"] = last.state == "off"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_passage_mode(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_passage_mode(False)


class TuyaBLEPersistentConnectionSwitch(TuyaBLELockEntity, SwitchEntity, RestoreEntity):
    """Keep BLE connection alive instead of disconnecting after idle timeout."""

    _attr_translation_key = "persistent_connection"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bluetooth-connect"

    @property
    def unique_id(self):
        return f"{self._mac}_persistent_connection"

    @property
    def is_on(self) -> bool:
        return self.coordinator.persistent_connection

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state == "on":
            await self.coordinator.async_set_persistent_connection(True)

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_persistent_connection(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_persistent_connection(False)
        self.async_write_ha_state()
