"""DataUpdateCoordinator for Tuya BLE lock."""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .device_profiles import parse_dp_value
from .event_collect import async_drain_event_pushes
from .event_flag import EventFlagTracker, parse_event_flag
from .temp_password_cleanup import async_cleanup_expired_passwords

# After a flag-triggered connect, keep draining for this long to catch the
# lock's repeated 0x8007 unlock push (it recurs ~every 6s). Covers the T+6 /
# T+12 repeats even when the connect lands a couple of seconds late.
EVENT_DRAIN_SECONDS = 15.0

_LOGGER = logging.getLogger(__name__)

# Check code — SYD8811 does NOT validate, H8 Pro rejects all-zeros.
DEFAULT_CHECK_CODE = b"12345678"

# Keep BLE connection alive for this long after the last operation. Kept short
# on purpose: the lock stops advertising while connected, so every second spent
# idling on an open session is a second we cannot hear the event flag. Just long
# enough to cover the post-connect event drain.
IDLE_DISCONNECT_SECONDS = 20

# Cooldown: don't retry connection if last failure was within this window
CONNECT_COOLDOWN_SECONDS = 600  # 10 minutes
# How often to connect just to sweep the lock's event queue when we are not
# holding a persistent connection. Unlocks wake us for free via the FD50
# advertisement flag, but failed attempts and the lockout (DP21) raise no
# flag and there is no local cloud path, so the only way to notice a lockout
# that is NOT followed by an unlock is to connect now and then and let the
# acked 0x8007 queue drain. One hour trades a lockout being seen within the
# hour against ~24 short connects a day (vs ~1300 for a persistent link).
ALARM_SWEEP_INTERVAL = timedelta(hours=1)
MAX_RECENT_EVENT_KEYS = 128
# A record whose lock timestamp is within this many seconds of now is a live
# unlock; anything older is backlog the lock held for us (history).
LIVE_EVENT_MAX_AGE = 300
# How many unlock records to keep in state for the history attribute.
MAX_RECENT_UNLOCKS = 50

# Minimum spacing between flag-triggered sessions. Kept short: a redundant
# session is cheap (the coordinator de-duplicates events on the lock's own
# timestamp), whereas a long window silently drops real second unlocks.
EVENT_FLAG_COOLDOWN_SECONDS = 15.0

# The lock holds the flag high 21-30s, so a second unlock in that window makes
# no rising edge. Re-open a session after this long of continuous high.
EVENT_FLAG_RETRIGGER_SECONDS = 30.0

# How often to re-read HA's cached advertisement. Costs no radio time; it only
# reads what the proxy already delivered, so a single missed callback does not
# lose the unlock.
ADVERTISEMENT_POLL_SECONDS = 2.0


class TuyaBLELockCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mac: str,
        device_name: str,
        device_data: dict,
        ble_device,
        session,
        profile: dict,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Tuya BLE Access {device_name}",
            update_interval=ALARM_SWEEP_INTERVAL,
        )
        self._entry = entry
        self._mac = mac
        self._device_name = device_name
        self._device_data = device_data
        self._session = session
        self._ble_device = ble_device
        self._op_lock = asyncio.Lock()
        self._temp_password_lock = asyncio.Lock()
        self._temp_cleanup_retry_at = 0.0
        self._profile = profile
        self._idle_timer: asyncio.TimerHandle | None = None
        self._listener_task: asyncio.Task | None = None
        self._persistent_connection: bool = False
        self._keepalive_task: asyncio.Task | None = None
        self._last_connect_failure: float = 0.0  # monotonic timestamp
        self._stopping: bool = False
        self._recent_event_keys: dict[tuple[int, int, bytes], None] = {}
        self._event_flag = EventFlagTracker(
            EVENT_FLAG_COOLDOWN_SECONDS, EVENT_FLAG_RETRIGGER_SECONDS
        )
        self._adv_seen: bool = False
        self._adv_count: int = 0
        self._adv_high_count: int = 0
        self._adv_trigger_count: int = 0
        self._adv_poll_task: asyncio.Task | None = None
        self._last_unlock_committed_ts: int = 0
        # Motor-transition ground truth: DP47 0->1 is a physical unlock and is
        # delivered far more reliably than the 0x8007 record. It owns the
        # timestamp and the bus event; the record only adds who/which finger.
        self._motor_unlock_at: float = 0.0
        self._motor_unlock_claimed: bool = True
        self._last_unlock_fire_at: float = 0.0

        # Listen for HA shutdown to cancel background tasks
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_ha_stop)

        # Build state dict from profile's state_map
        self.state: dict[str, Any] = {}
        for dp_str, mapping in profile.get("state_map", {}).items():
            key = mapping.get("key", "")
            if key and key != "_ignore" and key not in self.state:
                self.state[key] = None

        # Register push callback so DP reports update state in real-time
        self._session.set_dp_report_callback(self._process_dp_reports)

    @property
    def mac(self) -> str:
        return self._mac

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def device_data(self) -> dict:
        return self._device_data

    @property
    def profile(self) -> dict:
        return self._profile

    # DP id → (last_unlock label, CredentialStore cred_type for lookup).
    # cred_type matches the CRED_* constants in const.py — we use it to map
    # the hardware user id back to the HA member who enrolled that slot.
    _UNLOCK_METHOD_DPS: dict[int, tuple[str, int | None]] = {
        12: ("fingerprint", 0x03),      # CRED_FINGERPRINT
        13: ("password", 0x01),          # CRED_PASSWORD
        14: ("dynamic_code", None),
        15: ("card", 0x02),              # CRED_CARD
        16: ("mechanical_key", None),
        19: ("bluetooth", None),
        55: ("temporary_code", None),
        62: ("remote_phone", None),
        63: ("remote_voice", None),
        67: ("offline_code", None),
    }

    def apply_cloud_dps(self, cloud_dps: dict) -> None:
        """Populate coordinator.state from a cloud DP snapshot.

        cloud_dps comes from the Tuya mobile API — values are scalars
        (int/bool/str) or base64 blobs for RAW DPs. Unlike BLE DP reports
        (which are already bytes), these need type-aware interpretation
        before we can feed them through the profile's state_map.
        """
        state_map = self._profile.get("state_map", {})
        changed = False
        for dp_id_str, raw_val in (cloud_dps or {}).items():
            mapping = state_map.get(dp_id_str)
            if not mapping:
                continue
            key = mapping.get("key", "")
            parse_type = mapping.get("parse", "")
            if not key or key == "_ignore" or parse_type == "ignore":
                continue

            new_val: Any
            if parse_type == "bool":
                new_val = bool(raw_val) if not isinstance(raw_val, str) else (
                    raw_val.lower() in ("true", "1", "yes", "on")
                )
            elif parse_type == "int":
                try:
                    new_val = int(raw_val)
                except (TypeError, ValueError):
                    continue
            elif parse_type in ("enum_string", "raw_byte"):
                # enum_string → cloud sends the enum label directly
                # raw_byte → cloud also sends the enum label (BLE returns index)
                new_val = raw_val
            elif parse_type == "battery_state_enum":
                new_val = raw_val
            else:
                new_val = raw_val

            if self.state.get(key) != new_val:
                self.state[key] = new_val
                changed = True

        if changed:
            self.async_set_updated_data(self.state)

    def _process_dp_reports(self, dps: list[dict]) -> None:
        """Update state from DP reports using profile's state_map."""
        _LOGGER.debug("Processing %d DPs: %s", len(dps),
                        [(dp["id"], dp["raw"].hex()) for dp in dps])
        state_map = self._profile.get("state_map", {})
        changed = False
        for dp in dps:
            dp_id = dp["id"]
            dp_id_str = str(dp_id)
            # cmd=0x8007 event records carry the lock's own timestamp in
            # `event_ts`; cmd=0x8006 snapshots don't, so fall back to now.
            reported_event_ts = dp.get("event_ts")
            event_ts = reported_event_ts or int(time.time())
            # A motor run that just happened and is not yet matched to a record
            # is physical proof of a new unlock. Any unlock-method record in that
            # window belongs to it -- even if its key repeats an earlier event
            # (this lock reuses event_ts) or its ts looks older than the last
            # commit (this lock's clock is not monotonic).
            fresh_motor = (
                dp_id in self._UNLOCK_METHOD_DPS
                and not self._motor_unlock_claimed
                and (time.monotonic() - self._motor_unlock_at) < 20.0
            )
            motor_backed = False
            if reported_event_ts is not None:
                event_key = (dp_id, reported_event_ts, bytes(dp["raw"]))
                if event_key in self._recent_event_keys and not fresh_motor:
                    _LOGGER.debug(
                        "Ignoring duplicate event DP%d timestamp=%d raw=%s",
                        dp_id, reported_event_ts, dp["raw"].hex(),
                    )
                    continue
                if fresh_motor:
                    motor_backed = True
                    _LOGGER.debug(
                        "DP%d accepted on motor evidence (new unlock)", dp_id
                    )
                self._recent_event_keys[event_key] = None
                if len(self._recent_event_keys) > MAX_RECENT_EVENT_KEYS:
                    self._recent_event_keys.pop(next(iter(self._recent_event_keys)))

            # Track last unlock source (any unlock-method DP with non-zero user id)
            if dp_id in self._UNLOCK_METHOD_DPS:
                raw = dp["raw"]
                user_id = int.from_bytes(raw, "big") if raw else 0
                valid_identity = bool(user_id) if dp_id != 55 else len(raw) == 4 and user_id <= 254
                # Only real event frames (cmd=0x8007) carry an unlock; a status
                # snapshot (0x8006) merely echoes the last value and must not be
                # taken as a fresh unlock. And the lock replays several buffered
                # events per connect in arbitrary order, so an older one must
                # never overwrite a newer attribution -- gate on the timestamp.
                is_event = reported_event_ts is not None
                # The record's own timestamp is NOT usable for ordering: it is
                # correct only when the unlock happened while we were
                # connected (clock freshly synced) and ~20 h stale when the
                # lock was asleep -- exactly the "first unlock after idle"
                # case, which a ts-ordering guard silently dropped. Exact
                # repeats are stopped by the dedup key above; any record that
                # got past it is a new unlock.
                _LOGGER.debug(
                    "Unlock DP%d user=%s event_ts=%s committed=%s motor=%s -> %s",
                    dp_id, user_id, reported_event_ts,
                    self._last_unlock_committed_ts, motor_backed,
                    "COMMIT" if (valid_identity and is_event)
                    else ("skip:snapshot" if not is_event else "skip:no-user"),
                )
                if valid_identity and is_event:
                    if motor_backed:
                        self._motor_unlock_claimed = True
                    self._last_unlock_committed_ts = max(
                        self._last_unlock_committed_ts, reported_event_ts
                    )
                    method_label, cred_type = self._UNLOCK_METHOD_DPS[dp_id]
                    # Resolve hardware user_id -> HA member name via the
                    # credential store (only works for credentials enrolled
                    # through HA -- Tuya-app-enrolled ones stay as 'User <id>').
                    member_name: str | None = None
                    person_eid: str | None = None
                    credential_name: str | None = None
                    password_id: str | None = None
                    if dp_id == 55:
                        try:
                            attribution_ts = (self.state["last_unlock_time"] if motor_backed
                                              else reported_event_ts)
                            temp = self._entry.runtime_data.credential_store.resolve_temp_password(
                                self._mac, user_id, attribution_ts
                            )
                            if temp:
                                credential_name = temp.name
                                password_id = temp.password_id
                        except Exception as exc:
                            _LOGGER.debug("Temporary PIN lookup failed: %s", exc)
                    if cred_type is not None:
                        try:
                            runtime = self._entry.runtime_data
                            cred_store = runtime.credential_store
                            # The DP carries the credential slot (verified: two
                            # fingers of one person report 1 and 2), so the
                            # exact finger is resolvable.
                            member, credential_name = cred_store.resolve_unlock(
                                self._mac, cred_type, user_id
                            )
                            if member:
                                member_name = member.name
                                person_eid = getattr(member, "person_entity_id", None)
                        except Exception as exc:
                            _LOGGER.debug("Member lookup failed: %s", exc)
                    if member_name:
                        by = member_name
                    elif credential_name:
                        by = credential_name
                    elif cred_type is None:
                        # App/HA/mechanical unlocks carry no credential, so
                        # there is no person to attribute: naming a "User N"
                        # invents one. Report the method instead.
                        by = method_label.replace("_", " ").capitalize()
                    else:
                        by = f"User {user_id}"

                    # Once acked, the lock streams every record it still holds
                    # (it kept 48 of them for us), so a record is not "an
                    # unlock that just happened" unless we have evidence: the
                    # motor ran, or the lock's own timestamp is recent. The
                    # clock is synced on every connect, so a live record is
                    # stamped within seconds of real time; a backlog record is
                    # minutes to days old. Everything goes into the history.
                    age = int(time.time()) - reported_event_ts
                    live = motor_backed or abs(age) <= LIVE_EVENT_MAX_AGE
                    record = {
                        "time": reported_event_ts,
                        "method": method_label,
                        "user_id": user_id,
                        "by": by,
                        "person": person_eid,
                        "credential": credential_name,
                        "password_id": password_id,
                    }
                    history = list(self.state.get("recent_unlocks") or [])
                    history.insert(0, record)
                    self.state["recent_unlocks"] = history[:MAX_RECENT_UNLOCKS]
                    changed = True
                    _LOGGER.debug(
                        "Unlock record DP%d user=%s age=%ds -> %s",
                        dp_id, user_id, age, "LIVE" if live else "HISTORY",
                    )
                    if not live:
                        continue

                    # A live burst after a proxy hiccup can deliver two
                    # unlocks in either order; the newest one owns the
                    # "last unlock" fields, the other still gets its event.
                    prev_ts = self.state.get("last_unlock_event_ts") or 0
                    supersedes = motor_backed or reported_event_ts >= prev_ts
                    if supersedes:
                        self.state["last_unlock_method"] = method_label
                        self.state["last_unlock_user"] = user_id
                        # The motor transition already stamped this unlock if
                        # it arrived first; otherwise the lock's timestamp is
                        # the moment of the unlock itself (recent, see above).
                        if (time.monotonic() - self._motor_unlock_at) >= 20.0:
                            self.state["last_unlock_time"] = reported_event_ts
                        # Keep the lock's own event ts too: the sensor restores
                        # it after a restart so a replayed old event cannot
                        # commit as a fresh unlock.
                        self.state["last_unlock_event_ts"] = reported_event_ts
                        self.state["last_unlock_by"] = by
                        self.state["last_unlock_person"] = person_eid
                        self.state["last_unlock_credential"] = credential_name
                        self.state["last_unlock_password_id"] = password_id
                    else:
                        _LOGGER.debug(
                            "Unlock record DP%d user=%s ts=%d is older than the "
                            "current last unlock (%d): event only",
                            dp_id, user_id, reported_event_ts, prev_ts,
                        )
                    if dp_id == 55:
                        # A separate attribution event also fires when the motor
                        # already emitted the generic unlock with pending identity.
                        self.hass.bus.async_fire(
                            f"{DOMAIN}_temporary_code_used",
                            {"mac": self._mac, "name": self._device_name,
                             "timestamp": (self.state["last_unlock_time"] if motor_backed else reported_event_ts),
                             "lock_event_ts": reported_event_ts, "method": method_label,
                             "hw_id": user_id, "password_id": password_id,
                             "credential": credential_name,
                             "attributed": password_id is not None},
                        )
                    # When we saw the motor run, that transition already fired
                    # the bus event and the record only fills in who/which.
                    # When the unlock happened while we were not connected
                    # (first unlock after idle), the pushed record is all the
                    # evidence there is, so it must fire the event itself.
                    if not motor_backed and (
                        time.monotonic() - self._motor_unlock_at
                    ) >= 20.0:
                        self.hass.bus.async_fire(
                            f"{DOMAIN}_unlock",
                            {
                                "mac": self._mac,
                                "name": self._device_name,
                                "timestamp": reported_event_ts,
                                "source": "push",
                                "method": method_label,
                                "user_id": user_id,
                                "by": by,
                                "person": person_eid,
                                "credential": credential_name,
                                "password_id": password_id,
                            },
                        )

            # Alarm events carry the lock's timestamp too — expose it so the
            # Lock-alarm sensors show when it actually happened. Only event
            # records count (a status snapshot merely echoes the last value),
            # the newest record owns the time (backlog can arrive out of
            # order), and a recent one is announced on the bus.
            if dp_id == 21 and reported_event_ts is not None:  # alarm_lock
                prev_alarm_ts = self.state.get("last_alarm_time") or 0
                if reported_event_ts >= prev_alarm_ts:
                    self.state["last_alarm_time"] = reported_event_ts
                    changed = True
                alarm_age = int(time.time()) - reported_event_ts
                _LOGGER.debug(
                    "Alarm record raw=%s ts=%d age=%ds -> %s",
                    dp["raw"].hex(), reported_event_ts, alarm_age,
                    "LIVE" if abs(alarm_age) <= LIVE_EVENT_MAX_AGE else "HISTORY",
                )
                if abs(alarm_age) <= LIVE_EVENT_MAX_AGE:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_alarm",
                        {
                            "mac": self._mac,
                            "name": self._device_name,
                            "timestamp": reported_event_ts,
                            "alarm": parse_dp_value(
                                dp["raw"], state_map.get("21", {}).get("parse", "raw_byte")
                            ),
                        },
                    )

            mapping = state_map.get(dp_id_str)
            if not mapping:
                continue
            key = mapping.get("key", "")
            parse_type = mapping.get("parse", "raw_byte")
            if not key or key == "_ignore" or parse_type == "ignore":
                continue
            new_val = parse_dp_value(dp["raw"], parse_type)
            old_val = self.state.get(key)
            if old_val != new_val:
                self.state[key] = new_val
                changed = True
                # DP47 0->1: the motor ran, i.e. a physical unlock happened.
                # This is our reliable source for "when": stamp it now and tell
                # automations, without waiting for the 0x8007 record.
                # Strictly False->True: a first status read that happens to
                # land mid-motor (None->True) is not an unlock we witnessed.
                if key == "motor_state" and old_val is False and new_val:
                    now_mono = time.monotonic()
                    self._motor_unlock_at = now_mono
                    self._motor_unlock_claimed = False
                    self.state["last_unlock_time"] = int(time.time())
                    # Attribution belongs to the record that follows; until it
                    # arrives, do not keep showing the previous person.
                    for k in ("last_unlock_method", "last_unlock_user",
                              "last_unlock_by", "last_unlock_person",
                              "last_unlock_credential", "last_unlock_password_id"):
                        self.state[k] = None
                    _LOGGER.debug("Motor run on %s -> unlock at now", self._mac)
                    # Exactly one bus event per physical unlock: every motor
                    # run is one, so no time-based suppression.
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_unlock",
                        {
                            "mac": self._mac,
                            "name": self._device_name,
                            "timestamp": self.state["last_unlock_time"],
                            "source": "motor",
                            "attribution": "pending",
                        },
                    )
        if changed:
            self.async_set_updated_data(self.state)

    def _reset_idle_timer(self) -> None:
        """Reset the idle disconnect timer. Call after every operation."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if not self._persistent_connection:
            loop = self.hass.loop
            # Stay connected long enough to see the auto-lock DP 47 push
            # when auto_lock_time is configured higher than our default.
            delay = max(
                IDLE_DISCONNECT_SECONDS,
                int(self.state.get("auto_lock_time") or 0) + 10,
            )
            self._idle_timer = loop.call_later(
                delay, lambda: asyncio.ensure_future(self._idle_disconnect())
            )
        # Start background listener if not already running
        self._start_listener()

    def _start_listener(self) -> None:
        """Start background task that processes incoming BLE notifications."""
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = self.hass.async_create_task(self._notification_listener())

    async def _notification_listener(self) -> None:
        """Periodically drain notification buffer while BLE is connected.

        This catches unsolicited DP pushes (auto-lock motor_state, physical
        lock/unlock events, etc.) that arrive between explicit operations.
        """
        _LOGGER.debug("Notification listener started")
        try:
            while self._session.is_connected:
                await asyncio.sleep(2.0)
                if not self._session.is_connected:
                    break
                # Only drain if no operation is in progress (don't steal their data)
                if self._op_lock.locked():
                    continue
                if self._session._notif_buf:
                    async with self._session._lock:
                        raw = list(self._session._notif_buf)
                        self._session._notif_buf.clear()
                    if raw:
                        from .ble_protocol import parse_frames
                        frames = parse_frames(self._session._keys, raw)
                        if frames:
                            _LOGGER.debug("Listener: %d frames from %d notifications",
                                            len(frames), len(raw))
                            await self._session._handle_time_requests(frames)
                            self._session._dispatch_dp_reports(frames)
        except Exception as exc:
            _LOGGER.debug("Notification listener error: %s", exc)
        _LOGGER.debug("Notification listener stopped")

    async def _idle_disconnect(self) -> None:
        """Disconnect after idle timeout."""
        self._idle_timer = None
        if self._persistent_connection:
            return  # persistent mode — don't disconnect
        if self._session.is_connected:
            _LOGGER.debug("Idle timeout (%ds), disconnecting BLE", IDLE_DISCONNECT_SECONDS)
            await self._session.async_disconnect()
        # Listener will exit on its own when is_connected becomes False

    @property
    def persistent_connection(self) -> bool:
        return self._persistent_connection

    async def _on_ha_stop(self, event) -> None:
        """Cancel background tasks on HA shutdown."""
        self._stopping = True
        self._persistent_connection = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._idle_timer is not None:
            self._idle_timer.cancel()

    async def async_set_persistent_connection(self, enabled: bool) -> None:
        """Enable or disable persistent BLE connection."""
        self._persistent_connection = enabled
        if enabled:
            # Cancel any pending idle disconnect
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            # Start keepalive loop
            self._start_keepalive()
        else:
            # Stop keepalive loop
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
                self._keepalive_task = None
            # If connected, start idle timer so it disconnects normally
            if self._session.is_connected:
                self._reset_idle_timer()

    def _start_keepalive(self) -> None:
        """Start the keepalive loop that reconnects when BLE drops."""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        # Use background task so it doesn't block HA startup
        self._keepalive_task = self._entry.async_create_background_task(
            self.hass, self._keepalive_loop(),
            f"tuya_ble_access_keepalive_{self._mac}",
        )

    async def _keepalive_loop(self) -> None:
        """Keep reconnecting: this lock hangs up on its own after ~60s to save
        power, so 'persistent' really means 'reconnect the instant it drops'.

        Poll often while healthy (a dropped link is re-established within a few
        seconds, minimising the window in which an unlock is missed), and only
        back off when a reconnect actually fails so we don't hammer a lock that
        is out of range.
        """
        POLL_OK = 3.0            # seconds between health checks while connected
        backoff = POLL_OK
        # Small stagger so multiple locks don't reconnect in lockstep.
        await asyncio.sleep(random.uniform(1, 5))
        _LOGGER.debug("Persistent connection keepalive started for %s", self._mac)
        try:
            while self._persistent_connection and not self._stopping:
                if not self._session.is_connected:
                    _LOGGER.debug("Persistent connection: reconnecting %s...", self._mac)
                    async with self._op_lock:
                        try:
                            await self._async_ensure_connected()
                            await self._fetch_status()
                            self._start_listener()
                            backoff = POLL_OK  # healthy again
                        except Exception as exc:
                            _LOGGER.debug("Persistent reconnect failed for %s: %s", self._mac, exc)
                            backoff = min(max(backoff * 2, 10), 300)  # 10s..5min
                jitter = random.uniform(0.8, 1.2)
                await asyncio.sleep(backoff * jitter)
        except asyncio.CancelledError:
            pass
        _LOGGER.debug("Persistent connection keepalive stopped for %s", self._mac)

    async def _fetch_status(self) -> None:
        """Collect DP reports from the lock. Call while connected.

        On btScyChannel / protocol-5.0 firmwares (K3 BLE PRO 2) the
        CMD_DEVICE_STATUS (0x0003) response does include DPs — notably
        DP8 battery. On older firmwares (SYD8811, H8 Pro) it returns 0
        DPs and a trigger DP write is required instead. Try both.
        """
        try:
            await self._session.async_query_status()
        except Exception as exc:
            _LOGGER.debug("CMD_DEVICE_STATUS failed: %s", exc)

        battery_cfg = self._profile.get("entities", {}).get("battery_sensor")
        if battery_cfg:
            trigger_dp = battery_cfg.get("trigger_dp")
            trigger_hex = battery_cfg.get("trigger_payload")
            try:
                if trigger_dp and trigger_hex and self.state.get("battery_percent") is None:
                    trigger_payload = bytes.fromhex(trigger_hex)
                    await self._session.async_send_dp_raw(trigger_dp, trigger_payload)
                extra = await self._session._collect(timeout=2.0)
                _LOGGER.debug("Status collect: %d extra frames", len(extra))
                self._session._dispatch_dp_reports(extra)
            except Exception as exc:
                _LOGGER.warning("Status fetch failed: %s", exc)

    def seed_unlock_baseline(
        self, event_ts: int, dp_id: int | None = None, user_id: int | None = None
    ) -> None:
        """Restore the last committed unlock after a restart.

        The lock replays its last record on the first reconnect. Re-seeding the
        exact dedup key (dp, ts, value) makes that replay a duplicate hit, so it
        is neither committed nor fired as a phantom unlock.
        """
        if not event_ts:
            return
        if event_ts > self._last_unlock_committed_ts:
            self._last_unlock_committed_ts = event_ts
            self.state["last_unlock_event_ts"] = event_ts
        if dp_id is not None and user_id is not None:
            key = (dp_id, event_ts, int(user_id).to_bytes(4, "big"))
            self._recent_event_keys[key] = None

    def start_advertisement_poll(self, entry) -> None:
        """Begin polling HA's cached advertisement for this lock."""
        if self._adv_poll_task and not self._adv_poll_task.done():
            return
        self._adv_poll_task = entry.async_create_background_task(
            self.hass,
            self._async_poll_advertisements(),
            f"tuya_ble_access_adv_poll_{self._mac}",
        )

    async def _async_poll_advertisements(self) -> None:
        """Re-read HA's cached advertisement so one lost packet is not fatal.

        HA only invokes the advertisement callback when the payload *changes*,
        so a flag rise produces exactly one notification. If the proxy misses
        that single packet the unlock is gone: every later 0x5b advertisement
        counts as unchanged and never calls back. Polling the cached last
        advertisement gives repeated chances at zero radio cost -- it reads
        HA's own store, it does not scan or connect.
        """
        from homeassistant.components import bluetooth as _bt

        while not self._stopping:
            try:
                await asyncio.sleep(ADVERTISEMENT_POLL_SECONDS)
                if self._stopping:
                    return
                info = _bt.async_last_service_info(
                    self.hass, self._mac, connectable=False
                )
                if info is not None:
                    self._handle_service_info(info, source="poll")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.debug("Advertisement poll failed: %s", exc)

    def handle_advertisement(self, service_info) -> None:
        """React to the lock's advertisement flag.

        The lock raises a bit in its FD50 service data after a successful
        unlock. Advertisements arrive whether or not we listen, so this is a
        free wake signal: we open a session only when there is actually a
        record waiting, instead of holding one open permanently.
        """
        self._handle_service_info(service_info, source="push")

    def _handle_service_info(self, service_info, source: str) -> None:
        """Evaluate one advertisement (from the callback or the poll)."""
        config = self._profile.get("event_flag")
        if not config or self._stopping:
            return

        suffix = str(config.get("service_uuid", "fd50")).lower()
        payload = None
        for uuid, data in (service_info.service_data or {}).items():
            if suffix in str(uuid).lower():
                payload = bytes(data)
                break
        if payload is None:
            return

        if not self._adv_seen:
            self._adv_seen = True
            _LOGGER.debug(
                "Advertisement watcher active for %s (service data %s)",
                self._mac, payload.hex(),
            )

        flag = parse_event_flag(
            payload, int(config.get("byte", 0)), int(config.get("mask", 0x02))
        )
        # Diagnostic: without this we are blind to how many advertisements
        # actually reach HA and what the flag looked like, which is the only way
        # to tell a missed unlock from a missed advertisement.
        self._adv_count += 1
        if flag:
            self._adv_high_count += 1
        _LOGGER.debug(
            "ADV %s [%s] flag=%s connected=%s data=%s "
            "(seen=%d high=%d triggers=%d)",
            self._mac, source, flag, self._session.is_connected, payload.hex(),
            self._adv_count, self._adv_high_count, self._adv_trigger_count,
        )
        if not self._event_flag.update(flag, time.monotonic()):
            return
        self._adv_trigger_count += 1

        _LOGGER.debug(
            "Event flag raised for %s (service data %s), opening session",
            self._mac, payload.hex(),
        )
        self.hass.async_create_task(self._async_collect_pending_event())

    async def _async_collect_pending_event(self) -> None:
        """Open a short session so the lock can deliver its pending record.

        Connecting arms event push, and the idle timer keeps the session open
        for IDLE_DISCONNECT_SECONDS afterwards, which comfortably covers the
        window in which the lock still has the flag raised.
        """
        async with self._op_lock:
            try:
                await self._async_ensure_connected()
                # A re-trigger lands on a session that is often still open from
                # the previous unlock, with that session's idle timer about to
                # fire. Push the timer out first, or it disconnects us a few
                # seconds into the drain and the drain reports nothing.
                self._reset_idle_timer()
                # Listen for the live unlock push first: the lock repeats the
                # 0x8007 record every ~6s, so draining right after connect
                # catches it well before the status query would. Falls through
                # immediately once the record lands.
                outcome = await async_drain_event_pushes(
                    self._session, window=EVENT_DRAIN_SECONDS
                )
                _LOGGER.debug(
                    "Event drain for %s: %s", self._mac, outcome
                )
                if outcome == "disconnected":
                    return
                await self._fetch_status()
                self._reset_idle_timer()
            except Exception as exc:
                _LOGGER.debug(
                    "Event-flag session failed for %s: %s", self._mac, exc
                )

    async def async_one_shot_status(self) -> None:
        """Single-attempt status fetch at startup. No retries."""
        async with self._op_lock:
            try:
                if not await self._session.async_connect_single_attempt():
                    _LOGGER.debug("One-shot status: lock not responding, skipping")
                    return
                await self._fetch_status()
                await self._async_cleanup_temp_passwords()
                self._reset_idle_timer()
            except Exception as exc:
                _LOGGER.debug("One-shot status failed: %s", exc)
                await self._session.async_disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic sweep: connect, refresh status, drain queued events.

        This is the alarm safety net (see ALARM_SWEEP_INTERVAL). A status
        query collects the lock's pending 0x8007 records, which we now ack,
        so a wrong-credential alarm or lockout that raised no advertisement
        flag is still delivered at the next sweep. When a persistent
        connection is held this just refreshes over the open link."""
        # Skip if we recently failed — don't spam BLE on every 12h poll
        since_fail = time.monotonic() - self._last_connect_failure
        if not self._session.is_connected and since_fail < CONNECT_COOLDOWN_SECONDS:
            _LOGGER.debug(
                "Poll: skipping %s, last connect failed %ds ago (cooldown %ds)",
                self._mac, int(since_fail), CONNECT_COOLDOWN_SECONDS,
            )
            return self.state
        async with self._op_lock:
            try:
                await self._async_ensure_connected()
                await self._fetch_status()
                self._reset_idle_timer()
            except UpdateFailed:
                _LOGGER.debug("Poll: BLE connect failed for %s, returning stale state", self._mac)
            except Exception as exc:
                _LOGGER.debug("Poll error for %s: %s", self._mac, exc)
        return self.state

    async def _async_ensure_connected(self) -> None:
        if not self._session.is_connected:
            if not await self._session.async_connect():
                self._last_connect_failure = time.monotonic()
                raise UpdateFailed("BLE connection to lock failed")
        await self._async_cleanup_temp_passwords()

    async def _async_cleanup_temp_passwords(self) -> None:
        """Piggyback maintenance on normal connections, with bounded retries."""
        async with self._temp_password_lock:
            if time.monotonic() < self._temp_cleanup_retry_at or not self._session.is_connected:
                return
            dp = self._profile.get("services", {}).get("delete_temp_password", {}).get("dp")
            if dp is None:
                return
            store = self._entry.runtime_data.credential_store
            if not store.get_expired_temp_passwords(self._mac, time.time()):
                return
            self._temp_cleanup_retry_at = time.monotonic() + 60
            removed = await async_cleanup_expired_passwords(store, self._session, self._mac, dp)
            if removed:
                self.async_update_listeners()

    def _build_unlock_payload(self, action_unlock: bool) -> bytes:
        """Build unlock/lock DP RAW payload (19 bytes).

        Format confirmed by sniff of Tuya app on K3 BLE PRO 2:
          [ff ff]       member_id (0xFFFF = admin)
          [00 01]       version
          [8B ASCII]    check code (from cloud DP71, per-device)
          [01/00]       action: 01=unlock, 00=lock
          [4B BE]       Unix timestamp
          [00 01]       trailer (observed in app sniff)

        Note: the DP report echoes back with member/version swapped
        ([00 01][ff ff]) and trailer 00 00 — that's the report format,
        not the write format.
        """
        code_str = self._device_data.get("check_code") or ""
        code = (code_str.encode("ascii") + b"\x00" * 8)[:8] if code_str else (
            DEFAULT_CHECK_CODE + b"\x00" * 8
        )[:8]
        ts = int(time.time())
        payload = struct.pack(">HH", 0xFFFF, 1)
        payload += code
        payload += bytes([0x01 if action_unlock else 0x00])
        payload += struct.pack(">I", ts)
        payload += b"\x00\x01"
        return payload

    def _get_unlock_dp(self) -> int:
        """Get the unlock DP ID from profile."""
        lock_cfg = self._profile.get("entities", {}).get("lock", {})
        return lock_cfg.get("unlock_dp", 71)

    async def async_lock(self) -> None:
        async with self._op_lock:
            await self._async_ensure_connected()
            unlock_dp = self._get_unlock_dp()
            payload = self._build_unlock_payload(action_unlock=False)
            _LOGGER.debug("Sending lock command (DP %d RAW, %d bytes): %s", unlock_dp, len(payload), payload.hex())
            try:
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            except Exception as exc:
                _LOGGER.warning("Lock command failed, reconnecting: %s", exc)
                self._session.is_connected = False
                await self._async_ensure_connected()
                payload = self._build_unlock_payload(action_unlock=False)
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_unlock(self) -> None:
        async with self._op_lock:
            await self._async_ensure_connected()
            unlock_dp = self._get_unlock_dp()
            payload = self._build_unlock_payload(action_unlock=True)
            _LOGGER.debug("Sending unlock command (DP %d RAW, %d bytes): %s", unlock_dp, len(payload), payload.hex())
            try:
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            except Exception as exc:
                _LOGGER.warning("Unlock command failed, reconnecting: %s", exc)
                self._session.is_connected = False
                await self._async_ensure_connected()
                payload = self._build_unlock_payload(action_unlock=True)
                await self._session.async_send_dp_fire_and_forget(unlock_dp, 0, payload)
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_double_lock(self, enabled: bool) -> None:
        dl_cfg = self._profile.get("entities", {}).get("double_lock_switch")
        if not dl_cfg:
            _LOGGER.warning("Double lock not supported by this device profile")
            return
        dp = dl_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp_bool(dp, enabled)
            self.state["double_lock"] = enabled
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_volume(self, volume: int) -> None:
        vol_cfg = self._profile.get("entities", {}).get("volume_select")
        if not vol_cfg:
            _LOGGER.warning("Volume control not supported by this device profile")
            return
        dp = vol_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 4, bytes([volume]))  # type=4 (ENUM)
            self.state["volume"] = volume
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_passage_mode(self, passage_on: bool) -> None:
        """Toggle passage mode. Inverted from DP 33 (auto_lock).

        passage_on=True  → auto_lock=False → lock stays open
        passage_on=False → auto_lock=True  → lock auto-locks normally
        """
        pm_cfg = self._profile.get("entities", {}).get("passage_mode_switch")
        if not pm_cfg:
            _LOGGER.warning("Passage mode not supported by this device profile")
            return
        dp = pm_cfg["dp"]
        auto_lock_val = not passage_on
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp_bool(dp, auto_lock_val)
            self.state["auto_lock"] = auto_lock_val
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_enum_dp(self, dp: int, value: int, state_key: str) -> None:
        """Send an enum DP value (type=4) and update state."""
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 4, bytes([value]))
            self.state[state_key] = value
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()

    async def async_set_auto_lock_time(self, seconds: int) -> None:
        alt_cfg = self._profile.get("entities", {}).get("auto_lock_time_number")
        if not alt_cfg:
            _LOGGER.warning("Auto-lock time not supported by this device profile")
            return
        dp = alt_cfg["dp"]
        async with self._op_lock:
            await self._async_ensure_connected()
            await self._session.async_send_dp(dp, 2, struct.pack(">I", seconds))  # type=2 (VALUE)
            self.state["auto_lock_time"] = seconds
            await self._fetch_status()
            self.async_set_updated_data(self.state)
            self._reset_idle_timer()
