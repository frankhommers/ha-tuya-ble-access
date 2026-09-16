"""Sensor platform for Tuya BLE lock."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import TuyaBLELockEntity
from .models import TuyaBLELockData

_CRED_TYPE_LABEL = {1: "pin", 2: "card", 3: "fingerprint", 4: "face"}


async def async_setup_entry(hass, entry, async_add_entities):
    data: TuyaBLELockData = entry.runtime_data
    entities = []
    for mac, coordinator in data.coordinators.items():
        profile = coordinator.profile or {}
        entities_cfg = profile.get("entities", {})
        if "battery_sensor" in entities_cfg:
            entities.append(TuyaBLEBatterySensor(coordinator, entry))
        state_map = profile.get("state_map", {})
        has_alarm = any(m.get("key") == "alarm_lock" for m in state_map.values())
        if has_alarm:
            entities.append(TuyaBLEAlarmSensor(coordinator, entry))
            entities.append(TuyaBLELastAlarmTimeSensor(coordinator, entry))
        has_door = any(m.get("key") == "closed_opened" for m in state_map.values())
        if has_door:
            entities.append(TuyaBLEDoorSensor(coordinator, entry))
        # Last-unlock sensor: show method only on locks that expose at least
        # one unlock-method DP in their state_map.
        if any(
            (m.get("key") or "").startswith("unlock_") for m in state_map.values()
        ):
            entities.append(TuyaBLELastUnlockSensor(coordinator, entry))
            entities.append(TuyaBLELastUnlockBySensor(coordinator, entry))
            entities.append(TuyaBLELastUnlockCredentialSensor(coordinator, entry))
            entities.append(TuyaBLELastUnlockTimeSensor(coordinator, entry))
        # Credentials overview: registered slots (from the HA store) plus the
        # lock's own occupied/unknown slots from the last sync_credentials run.
        if profile.get("services"):
            entities.append(TuyaBLECredentialsSensor(coordinator, entry))
    if entities:
        async_add_entities(entities)


BATTERY_STATE_TO_PERCENT = {
    "high": 100,
    "medium": 50,
    "low": 25,
    "exhausted": 5,
}


class TuyaBLEBatterySensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"{self._mac}_battery"

    @property
    def native_value(self) -> int | None:
        pct = self.coordinator.state.get("battery_percent")
        if pct is not None:
            return pct
        state = self.coordinator.state.get("battery_state")
        if state:
            return BATTERY_STATE_TO_PERCENT.get(state)
        alarm = self.coordinator.state.get("alarm_lock")
        if alarm == "low_battery" or alarm == 10:
            return 10
        return None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("battery_percent") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                try:
                    self.coordinator.state["battery_percent"] = int(float(last.state))
                except (ValueError, TypeError):
                    pass


_ALARM_LOCK_MAP = [
    "wrong_finger", "wrong_password", "wrong_card", "wrong_face",
    "tongue_bad", "too_hot", "unclosed_time", "tongue_not_out",
    "pry", "key_in", "low_battery", "power_off", "shock", "defense",
]


class TuyaBLEAlarmSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_translation_key = "lock_alarm"
    _attr_icon = "mdi:alert-circle"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _ALARM_LOCK_MAP

    @property
    def unique_id(self):
        return f"{self._mac}_alarm"

    @property
    def native_value(self) -> str | None:
        val = self.coordinator.state.get("alarm_lock")
        if val is None:
            return None
        if isinstance(val, int) and 0 <= val < len(_ALARM_LOCK_MAP):
            return _ALARM_LOCK_MAP[val]
        return str(val)

    @property
    def extra_state_attributes(self) -> dict:
        alarm = self.coordinator.state.get("alarm_lock")
        if not alarm:
            return {}
        attrs = {"raw_value": alarm}
        ts = self.coordinator.state.get("last_alarm_time")
        if ts:
            attrs["timestamp"] = ts
            from datetime import datetime, timezone
            attrs["timestamp_local"] = datetime.fromtimestamp(
                ts, tz=timezone.utc
            ).astimezone().isoformat()
        return attrs


_DOOR_STATE_MAP = {
    0: "unknown",
    1: "open",
    2: "closed",
    "unknown": "unknown",
    "open": "open",
    "closed": "closed",
}


class TuyaBLEDoorSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_translation_key = "door"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["unknown", "open", "closed"]
    _attr_icon = "mdi:door"

    @property
    def unique_id(self):
        return f"{self._mac}_door"

    @property
    def native_value(self) -> str | None:
        val = self.coordinator.state.get("closed_opened")
        if val is None:
            return None
        return _DOOR_STATE_MAP.get(val, str(val))

    @property
    def icon(self) -> str:
        val = self.native_value
        if val == "open":
            return "mdi:door-open"
        if val == "closed":
            return "mdi:door-closed"
        return "mdi:door"


UNLOCK_METHOD_OPTIONS = [
    "fingerprint", "password", "dynamic_code", "card",
    "mechanical_key", "bluetooth", "temporary_code",
    "remote_phone", "remote_voice", "offline_code",
]


class TuyaBLELastUnlockSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    _attr_translation_key = "last_unlock"
    _attr_icon = "mdi:key-variant"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = UNLOCK_METHOD_OPTIONS

    @property
    def unique_id(self):
        return f"{self._mac}_last_unlock"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.get("last_unlock_method")

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        user = self.coordinator.state.get("last_unlock_user")
        by = self.coordinator.state.get("last_unlock_by")
        person = self.coordinator.state.get("last_unlock_person")
        credential = self.coordinator.state.get("last_unlock_credential")
        ts = self.coordinator.state.get("last_unlock_time")
        if user is not None:
            attrs["user_id"] = user
        if by:
            attrs["by"] = by
        if person:
            attrs["person_entity_id"] = person
        if credential:
            attrs["credential"] = credential
        password_id = self.coordinator.state.get("last_unlock_password_id")
        if password_id:
            attrs["password_id"] = password_id
        if ts is not None:
            attrs["timestamp"] = ts
            from datetime import datetime, timezone
            attrs["timestamp_local"] = datetime.fromtimestamp(
                ts, tz=timezone.utc
            ).astimezone().isoformat()
        ev = self.coordinator.state.get("last_unlock_event_ts")
        if ev is not None:
            attrs["lock_event_ts"] = ev
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Seed the coordinator with the last committed lock event ts so the
        # replay the lock sends on the first reconnect after a restart is
        # recognised as old instead of committed as a fresh unlock.
        if last is not None:
            try:
                ev = int(last.attributes.get("lock_event_ts") or 0)
            except (TypeError, ValueError):
                ev = 0
            if ev:
                method = last.state if last.state not in (None, "unknown", "unavailable") else None
                dp_id = next(
                    (d for d, (lbl, _c) in self.coordinator._UNLOCK_METHOD_DPS.items()
                     if lbl == method),
                    None,
                )
                try:
                    saved_uid = last.attributes.get("user_id")
                    uid = int(saved_uid) if saved_uid is not None else None
                except (TypeError, ValueError):
                    uid = None
                self.coordinator.seed_unlock_baseline(ev, dp_id, uid)
        if self.coordinator.state.get("last_unlock_method") is None:
            if last and last.state not in (None, "unknown", "unavailable"):
                self.coordinator.state["last_unlock_method"] = last.state
                self.coordinator.state["last_unlock_password_id"] = last.attributes.get("password_id")




class TuyaBLELastUnlockBySensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    """Resolves the last unlocker's hardware user id to the HA member name.

    Falls back to 'User <id>' when the credential was not enrolled or imported
    through Home Assistant.
    """
    _attr_translation_key = "last_unlock_by"
    _attr_icon = "mdi:account-key"

    @property
    def unique_id(self):
        return f"{self._mac}_last_unlock_by"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.state.get("last_unlock_by")

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        person = self.coordinator.state.get("last_unlock_person")
        user = self.coordinator.state.get("last_unlock_user")
        credential = self.coordinator.state.get("last_unlock_credential")
        if person:
            attrs["person_entity_id"] = person
        if user is not None:
            attrs["user_id"] = user
        if credential:
            attrs["credential"] = credential
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("last_unlock_by") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                self.coordinator.state["last_unlock_by"] = last.state


class TuyaBLELastUnlockCredentialSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    """Shows the friendly name of the credential used for the last unlock.

    State is the fingerprint/card/PIN label itself (e.g. 'Left ring') so it
    can be displayed directly on a dashboard tile. Falls back to the raw
    hardware user id when the credential was not enrolled through HA.
    """

    _attr_translation_key = "last_unlock_credential"
    _attr_icon = "mdi:fingerprint"

    @property
    def unique_id(self):
        return f"{self._mac}_last_unlock_credential"

    @property
    def native_value(self) -> str | None:
        credential = self.coordinator.state.get("last_unlock_credential")
        if credential:
            return credential
        user = self.coordinator.state.get("last_unlock_user")
        if user:
            return f"User {user}"
        return None

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        user = self.coordinator.state.get("last_unlock_user")
        by = self.coordinator.state.get("last_unlock_by")
        method = self.coordinator.state.get("last_unlock_method")
        if user is not None:
            attrs["user_id"] = user
        if by:
            attrs["by"] = by
        if method:
            attrs["method"] = method
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("last_unlock_credential") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                self.coordinator.state["last_unlock_credential"] = last.state


class TuyaBLECredentialsSensor(TuyaBLELockEntity, SensorEntity):
    """Registered credentials for this lock, with lock-side slot occupancy.

    State is the number of credentials Home Assistant has registered for the
    lock. Attributes list each registered slot (hw_id, type, name, person) and,
    once sync_credentials has run, the slots the LOCK itself reports occupied —
    including 'unknown' orphan slots HA has no attribution for.
    """

    _attr_translation_key = "credentials"
    _attr_icon = "mdi:fingerprint"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"{self._mac}_credentials"

    def _store(self):
        return (self.hass.data.get(DOMAIN) or {}).get("credential_store")

    @property
    def native_value(self):
        store = self._store()
        if store is None:
            return None
        return len(store.get_credentials_for_lock(self._mac))

    @property
    def extra_state_attributes(self):
        store = self._store()
        if store is None:
            return None
        members = {m.member_id: m for m in store.get_members()}
        registered = []
        for c in store.get_credentials_for_lock(self._mac):
            member = members.get(c.member_id)
            registered.append({
                "hw_id": c.hw_id,
                "type": _CRED_TYPE_LABEL.get(c.cred_type, str(c.cred_type)),
                "name": c.name,
                "person": getattr(member, "person_entity_id", None),
                "member": getattr(member, "name", None),
            })
        registered.sort(key=lambda e: (e["type"], e["hw_id"]))
        attrs = {"registered": registered}
        attrs.update(store.get_temp_password_overview(self._mac))
        # Last sync_credentials snapshot (lock truth), if available.
        last_sync = getattr(self.coordinator, "last_credential_sync", None)
        if last_sync:
            occupied = []
            unknown = []
            for ctype, entries in last_sync.items():
                for e in entries:
                    occupied.append({"type": ctype, "hw_id": e["hw_id"]})
                    if not e.get("known"):
                        unknown.append({"type": ctype, "hw_id": e["hw_id"]})
            attrs["lock_occupied"] = occupied
            attrs["lock_unknown"] = unknown
        return attrs


class TuyaBLELastUnlockTimeSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    """When the last unlock happened, as a proper timestamp.

    Uses the lock's own event timestamp from the 0x8007 record, so Home
    Assistant can render it as "x minutes ago" and use it in automations.
    """

    _attr_translation_key = "last_unlock_time"
    _attr_icon = "mdi:clock-check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self):
        return f"{self._mac}_last_unlock_time"

    @property
    def native_value(self):
        ts = self.coordinator.state.get("last_unlock_time")
        if not ts:
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict:
        """Recent unlock records as the lock handed them over (newest first).

        Once acknowledged, the lock streams every record it still holds, so
        this is the closest thing to the app's history that exists without
        the cloud. Each entry: time (ISO, local), method, user_id, by,
        person, credential.
        """
        from datetime import datetime, timezone
        out = []
        for rec in self.coordinator.state.get("recent_unlocks") or []:
            entry = dict(rec)
            try:
                entry["time"] = datetime.fromtimestamp(
                    int(rec["time"]), tz=timezone.utc
                ).astimezone().isoformat()
            except (KeyError, ValueError, TypeError, OSError):
                pass
            out.append(entry)
        return {"recent_unlocks": out}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = None
        if self.coordinator.state.get("last_unlock_time") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(last.state)
                    self.coordinator.state["last_unlock_time"] = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass
        if not self.coordinator.state.get("recent_unlocks"):
            last = last or await self.async_get_last_state()
            restored = (last.attributes.get("recent_unlocks") if last else None) or []
            from datetime import datetime
            recs = []
            for rec in restored:
                try:
                    entry = dict(rec)
                    entry["time"] = int(datetime.fromisoformat(rec["time"]).timestamp())
                    recs.append(entry)
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            if recs:
                self.coordinator.state["recent_unlocks"] = recs


class TuyaBLELastAlarmTimeSensor(TuyaBLELockEntity, SensorEntity, RestoreEntity):
    """When the last lock alarm (wrong finger/password, pry, ...) happened.

    Uses the lock's own timestamp from the 0x8007 alarm record. Alarms do
    not raise the advertisement flag, so they typically reach us at the next
    connect; the lock's timestamp is still the moment it happened.
    """

    _attr_translation_key = "last_alarm_time"
    _attr_icon = "mdi:clock-alert"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self):
        return f"{self._mac}_last_alarm_time"

    @property
    def native_value(self):
        ts = self.coordinator.state.get("last_alarm_time")
        if not ts:
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.state.get("last_alarm_time") is None:
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(last.state)
                    self.coordinator.state["last_alarm_time"] = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass
