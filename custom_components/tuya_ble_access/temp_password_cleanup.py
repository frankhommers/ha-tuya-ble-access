"""Remove expired temporary PINs over an already established BLE connection."""

import logging
import time

_LOGGER = logging.getLogger(__name__)


async def async_cleanup_expired_passwords(store, session, lock_id, dp_id) -> int:
    """Caller serializes this with creation; never connect or retry in a loop.

    Tuya DP52 responds with [hardware ID, status]: 0 success, 2 already absent.
    https://developer.tuya.com/en/docs/iot/ble?id=K9ow3vcpn71ua
    """
    if dp_id is None or not session.is_connected:
        return 0
    removed = 0
    for candidate in store.get_expired_temp_passwords(lock_id, time.time()):
        if not session.is_connected:
            break
        # Recheck after earlier requests yielded: reset or another operation may
        # have changed the store. A superseded hardware ID must never be deleted.
        current = next((rec for rec in store.get_expired_temp_passwords(lock_id, time.time())
                        if rec.password_id == candidate.password_id), None)
        if current is None:
            continue
        try:
            result = await session.async_send_dp_raw(dp_id, bytes([current.hw_id]))
            raw = result.get("raw") if isinstance(result, dict) else None
            if (not isinstance(raw, bytes) or len(raw) != 2
                    or result.get("id") != dp_id or result.get("type") != 0
                    or raw[0] != current.hw_id):
                _LOGGER.warning("Temporary PIN cleanup not confirmed for %s slot %s; will retry on a later connection",
                                lock_id, current.hw_id)
                # Avoid extending a broken session with further maintenance.
                break
            if raw[1] not in (0, 2):
                _LOGGER.warning("Temporary PIN deletion rejected for %s slot %s (status %s); will retry later",
                                lock_id, current.hw_id, raw[1])
                continue
            if await store.async_archive_temp_password(current.password_id, time.time()):
                removed += 1
        except Exception as err:
            _LOGGER.warning("Temporary PIN cleanup failed for %s slot %s (%s); will retry later",
                            lock_id, current.hw_id, type(err).__name__)
            break
    return removed
