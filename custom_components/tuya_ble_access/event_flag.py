"""Advertisement event flag: connect only when the lock has something to say.

Locks on this firmware raise a bit in their FD50 advertisement service data
after a successful unlock and clear it again roughly half a minute later.
Because the lock advertises anyway, watching that bit is free — it needs no
GATT session and costs the lock no battery, unlike a persistent connection.

Verified on a K3 BLE PRO 2 (2026-08-29): resting service data starts with 0x59
and rises to 0x5b for 21-30s after each successful unlock, with the remaining
11 bytes unchanged. Three unlocks, no false positives in 90 minutes of idle
observation.

Deliberately free of Home Assistant imports so it can be tested offline.
"""

from __future__ import annotations

# Advertisements are lossy (~35% missed in practice), so a gap can look like the
# flag dropping. A short cooldown still absorbs that, and a spurious extra
# connect is cheap: the coordinator de-duplicates on the lock's own event
# timestamp, so re-reading an event can never double-count it. A long cooldown
# would instead drop *real* second unlocks, which is the worse failure.
DEFAULT_COOLDOWN_SECONDS = 15.0

# The flag stays high 21-30s after an unlock, so a second unlock inside that
# window produces no rising edge at all. Look again after this long of
# continuous high rather than losing that event.
DEFAULT_RETRIGGER_SECONDS = 30.0


def parse_event_flag(
    service_data: bytes, byte_index: int, mask: int
) -> bool | None:
    """Read the event flag out of FD50 service data.

    Returns None when the advertisement is too short to hold the flag, which
    must be treated as "unknown" rather than "low".
    """
    if service_data is None or byte_index >= len(service_data):
        return None
    return bool(service_data[byte_index] & mask)


class EventFlagTracker:
    """Turn a stream of flag readings into "connect now" decisions.

    Fires on a rising edge, and again if the flag simply stays high for
    `retrigger_seconds` — the lock holds it up for 21-30s, so a second unlock
    inside that window never produces an edge and would otherwise be lost.
    Unknown readings hold the current state so a malformed or truncated
    advertisement cannot look like the flag dropping.
    """

    def __init__(
        self,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        retrigger_seconds: float = DEFAULT_RETRIGGER_SECONDS,
    ):
        self._cooldown = cooldown_seconds
        self._retrigger = retrigger_seconds
        self._high = False
        self._last_trigger: float | None = None

    @property
    def is_high(self) -> bool:
        return self._high

    def update(self, flag: bool | None, now: float) -> bool:
        """Feed one reading. Returns True when a session should be opened."""
        if flag is None:
            return False

        was_high = self._high
        self._high = flag

        if not flag:
            return False

        if was_high:
            # Still the same episode. The lock may nevertheless have logged
            # another unlock, which produces no edge — so look again once the
            # flag has been up for a while.
            if (
                self._last_trigger is None
                or now - self._last_trigger < self._retrigger
            ):
                return False
            self._last_trigger = now
            return True

        if (
            self._last_trigger is not None
            and now - self._last_trigger < self._cooldown
        ):
            return False

        self._last_trigger = now
        return True
