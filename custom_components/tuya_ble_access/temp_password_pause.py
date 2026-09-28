"""Confirmed DP53 schedule changes, retaining the original validity window.

The no-weekdays strategy must be explicitly enabled in the device profile only
after a physical deny/restore test. DP53 support alone is not sufficient.
"""

import struct
import time


class TempPasswordPauseError(ValueError):
    """A pause/resume failure with a translated service error key."""

    def __init__(self, key):
        self.translation_key = key
        super().__init__(key)


def build_schedule_update(hw_id, effective_ts, expiry_ts, paused):
    """Never replace PIN digits or extend the original expiry."""
    if type(hw_id) is not int or not 0 <= hw_id <= 254:
        raise ValueError("Invalid temporary PIN hardware ID")
    if (type(paused) is not bool or any(type(v) is not int or not 0 <= v <= 0xFFFFFFFF
                                      for v in (effective_ts, expiry_ts))
            or effective_ts >= expiry_ts):
        raise ValueError("Invalid temporary PIN validity")
    # Weekly, no enabled weekdays, 00:00..23:59. Resume the nonrecurring window
    # used by our create_temp_password service, without changing its dates.
    recurrence = bytes.fromhex("02000000000000173b") if paused else bytes(9)
    return (bytes([hw_id, 1]) + struct.pack(">II", effective_ts, expiry_ts)
            + recurrence + bytes(2))


def current_password(store, lock_id, password_id):
    records = store.get_temp_passwords_for_lock(lock_id)
    rec = next((r for r in records if r.password_id == password_id), None)
    if (rec is None or type(rec.hw_id) is not int or not 0 <= rec.hw_id <= 254
            or sum(r.hw_id == rec.hw_id for r in records) != 1):
        raise TempPasswordPauseError("temp_password_not_current")
    if rec.expiry_ts <= time.time():
        raise TempPasswordPauseError("temp_password_expired")
    return rec


async def async_set_paused(store, session, lock_id, password_id, dp, paused):
    """Caller holds operation and temporary-password locks with BLE connected."""
    rec = current_password(store, lock_id, password_id)
    payload = build_schedule_update(rec.hw_id, rec.effective_ts, rec.expiry_ts, paused)
    # On timeout, cancellation or process restart, the device may have applied
    # the write. Persist unknown first, rather than reporting stale certainty.
    await store.async_set_temp_password_pause_state(rec, "unknown", paused)
    response = await session.async_send_dp_raw(dp, payload)
    if (not isinstance(response, dict) or response.get("id") != dp
            or response.get("type") != 0
            or not isinstance(response.get("raw"), bytes)
            or len(response["raw"]) != 2 or response["raw"][0] != rec.hw_id):
        raise TempPasswordPauseError("temp_password_unconfirmed")
    if response["raw"][1] != 0:
        raise TempPasswordPauseError("temp_password_pause_rejected")
    # A reset/slot replacement during the await must not mark another PIN.
    fresh = current_password(store, lock_id, password_id)
    if fresh.hw_id != rec.hw_id:
        raise TempPasswordPauseError("temp_password_not_current")
    state = "paused" if paused else "active"
    await store.async_set_temp_password_pause_state(rec, state, paused)
    return {"password_id": rec.password_id, "name": rec.name, "pause_state": state,
            "effective_ts": rec.effective_ts, "expiry_ts": rec.expiry_ts}
