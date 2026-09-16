"""Lock platform for Tuya BLE lock.

Tracks actual locked/unlocked state via DP reports (motor_state transitions)
and passage mode sync (auto_lock DP).  State is persisted across restarts
via RestoreEntity.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        entities.append(TuyaBLELock(coordinator, entry))
    if entities:
        async_add_entities(entities)


class TuyaBLELock(TuyaBLELockEntity, LockEntity, RestoreEntity):
    """Tracks locked/unlocked state from DP 47 (motor_state).

    DP 47 = 1 → motor in the 'open' position  → is_locked = False
    DP 47 = 0 → motor in the 'idle' position  → is_locked = True

    When passage mode is off (auto_lock=true), DP 47 flips 1 → 0 after the
    configured delay and the lock card goes back to locked automatically.
    When passage mode is on (auto_lock=false) DP 47 stays at 1 until the
    user presses Lock. We no longer conflate the auto_lock *setting* with
    the physical motor state — they are independent.
    """

    _attr_name = None
    _attr_unique_id_suffix = "lock"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._unlocking = False
        self._locking = False
        self._optimistic: bool | None = None  # used between send and first DP 47 echo

    @property
    def unique_id(self) -> str:
        return f"{self._mac}_lock"

    @property
    def icon(self) -> str:
        return "mdi:lock" if self.is_locked else "mdi:lock-open"

    @property
    def is_locked(self) -> bool | None:
        motor = self.coordinator.state.get("motor_state")
        if motor is not None:
            return not bool(motor)
        return self._optimistic  # None until we know

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ble_connected": self.coordinator._session.is_connected}

    @property
    def is_locking(self) -> bool:
        return self._locking

    @property
    def is_unlocking(self) -> bool:
        return self._unlocking

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("motor_state") is None:
            last = await self.async_get_last_state()
            if last and last.state in ("locked", "unlocked"):
                # Seed motor_state so is_locked returns something sensible
                # immediately; real push reports will correct it.
                self.coordinator.state["motor_state"] = last.state == "unlocked"

    async def async_lock(self, **kwargs) -> None:
        self._locking = True
        self._optimistic = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_lock()
        finally:
            self._locking = False
            self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        self._unlocking = True
        self._optimistic = False
        self.async_write_ha_state()
        try:
            await self.coordinator.async_unlock()
        finally:
            self._unlocking = False
            self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        # Once a real motor_state arrives, drop the optimistic override.
        if self.coordinator.state.get("motor_state") is not None:
            self._optimistic = None
        super()._handle_coordinator_update()
