"""Select platform for Tuya BLE lock."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.components import bluetooth
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData

def _option_key(value: str) -> str:
    """Accept old English labels when restoring pre-i18n entity states."""
    return value.lower().replace(" ", "_")


_ENUM_SELECTS = [
    {
        "config_key": "language_select",
        "translation_key": "language",
        "icon": "mdi:translate",
        "state_key": "language",
        "uid_suffix": "language",
        "default_dp": 28,
    },
    {
        "config_key": "unlock_mode_select",
        "translation_key": "unlock_mode",
        "icon": "mdi:lock-smart",
        "state_key": "unlock_switch",
        "uid_suffix": "unlock_mode",
        "default_dp": 34,
    },
]


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        entities.append(TuyaBLEBluetoothProxySelect(coordinator, entry))
        profile = coordinator.profile or {}
        entities_cfg = profile.get("entities", {})
        vol_cfg = entities_cfg.get("volume_select")
        if vol_cfg:
            options = [
                o for o in vol_cfg.get("options", ["mute", "normal"])
            ]
            entities.append(TuyaBLEVolumeSelect(coordinator, entry, options))
        for spec in _ENUM_SELECTS:
            cfg = entities_cfg.get(spec["config_key"])
            if cfg:
                entities.append(TuyaBLEEnumSelect(coordinator, entry, cfg, spec))
    if entities:
        async_add_entities(entities)


class TuyaBLEVolumeSelect(TuyaBLELockEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, options: list[str]):
        super().__init__(coordinator, entry)
        self._attr_options = options
        self._label_to_val = {label: idx for idx, label in enumerate(options)}
        self._val_to_label = {idx: label for idx, label in enumerate(options)}

    @property
    def unique_id(self):
        return f"{self._mac}_volume"

    @property
    def current_option(self) -> str | None:
        vol = self.coordinator.state.get("volume")
        if vol is None:
            return None
        return self._val_to_label.get(vol, f"unknown_{vol}")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("volume") is None:
            last = await self.async_get_last_state()
            if last and (option := _option_key(last.state)) in self._label_to_val:
                self.coordinator.state["volume"] = self._label_to_val[option]

    async def async_select_option(self, option: str) -> None:
        value = self._label_to_val.get(_option_key(option))
        if value is not None:
            await self.coordinator.async_set_volume(value)


class TuyaBLEEnumSelect(TuyaBLELockEntity, SelectEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, cfg: dict, spec: dict):
        self._attr_translation_key = spec["translation_key"]
        self._attr_icon = spec["icon"]
        self._state_key = spec["state_key"]
        self._uid_suffix = spec["uid_suffix"]
        self._dp = cfg.get("dp", spec["default_dp"])
        raw_options = cfg.get("options", [])
        super().__init__(coordinator, entry)
        self._attr_options = list(raw_options)
        self._label_to_val = {
            o: idx for idx, o in enumerate(raw_options)
        }
        self._val_to_label = {
            idx: o for idx, o in enumerate(raw_options)
        }

    @property
    def unique_id(self):
        return f"{self._mac}_{self._uid_suffix}"

    @property
    def current_option(self) -> str | None:
        val = self.coordinator.state.get(self._state_key)
        if val is None:
            return None
        return self._val_to_label.get(val, f"unknown_{val}")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get(self._state_key) is None:
            last = await self.async_get_last_state()
            if last and (option := _option_key(last.state)) in self._label_to_val:
                self.coordinator.state[self._state_key] = self._label_to_val[option]

    async def async_select_option(self, option: str) -> None:
        value = self._label_to_val.get(_option_key(option))
        if value is not None:
            await self.coordinator.async_set_enum_dp(self._dp, value, self._state_key)


class TuyaBLEBluetoothProxySelect(TuyaBLELockEntity, SelectEntity):
    """Choose a strict route per lock; automatic retains HA's normal routing."""

    _attr_translation_key = "bluetooth_proxy"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self):
        return f"{self._mac}_bluetooth_proxy"

    @property
    def available(self):
        # Changing a broken route must remain possible while the lock is offline.
        return True

    def _routes(self):
        routes = {"automatic": None}
        for scanner in bluetooth.async_current_scanners(self.hass):
            if scanner.connectable:
                routes[f"{scanner.name} [{scanner.source}]"] = scanner.source
        source = self.coordinator.device_data.get("bluetooth_source")
        if source and source not in routes.values():
            routes[source] = source
        return routes

    @property
    def options(self):
        return list(self._routes())

    @property
    def current_option(self):
        source = self.coordinator.device_data.get("bluetooth_source")
        return next(label for label, value in self._routes().items() if value == source)

    async def async_select_option(self, option):
        routes = self._routes()
        if option not in routes:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="bluetooth_proxy_unavailable",
            )
        source = routes[option]
        if source:
            from bleak import BleakClient
            from .bluetooth_route import client_for_source
            try:
                client_for_source(BleakClient, source)
            except Exception as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="bluetooth_proxy_unsupported",
                ) from exc
        await self.coordinator.async_set_bluetooth_source(source)
        self.async_write_ha_state()
