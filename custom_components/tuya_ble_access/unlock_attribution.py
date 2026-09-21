"""Correlate a fresh Bluetooth unlock report with its HA command initiator.

Hardware user numbers never identify Home Assistant users. Correlation is local,
short-lived and deliberately conservative when commands overlap.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnlockInitiator:
    name: str
    ha_user_id: str | None = None
    person_entity_id: str | None = None
    context: Any = None

    def attributes(self) -> dict:
        return {
            "ha_user_id": self.ha_user_id,
            "context_id": getattr(self.context, "id", None),
            "parent_id": getattr(self.context, "parent_id", None),
            "initiator_source": "home_assistant",
        }


@dataclass
class UnlockRequest:
    initiator: UnlockInitiator
    sent_at: int
    monotonic_at: float
    claimed: bool = False


class UnlockAttribution:
    """One attribution per command; no inference from old or ambiguous reports."""

    def __init__(self):
        self._requests: list[UnlockRequest] = []

    def _expire(self, now: float) -> None:
        self._requests = [r for r in self._requests if 0 <= now - r.monotonic_at <= 30]

    def start(self, initiator: UnlockInitiator, sent_at: int, now: float) -> UnlockRequest:
        self._expire(now)
        request = UnlockRequest(initiator, sent_at, now)
        self._requests.append(request)
        return request

    def cancel(self, request: UnlockRequest) -> None:
        # A received physical event remains valid even if later I/O failed.
        if not request.claimed:
            self._requests = [r for r in self._requests if r is not request]

    def match(self, event_ts: int, now: float) -> UnlockInitiator | None:
        self._expire(now)
        candidates = [r for r in self._requests if 0 <= event_ts - r.sent_at <= 5]
        # Keep claimed requests in the ambiguity check: a repeated record must
        # not borrow the identity of a newer command in the same time window.
        if len(candidates) != 1 or candidates[0].claimed:
            return None
        candidates[0].claimed = True
        return candidates[0].initiator


async def async_resolve_initiator(hass, context) -> UnlockInitiator:
    """Resolve only the user explicitly supplied by HA's service-call context."""
    user_id = getattr(context, "user_id", None)
    if user_id is None:
        return UnlockInitiator("Home Assistant", context=context)
    user = await hass.auth.async_get_user(user_id)
    name = (user.name if user else None) or "Home Assistant"
    people = [state.entity_id for state in hass.states.async_all("person")
              if state.attributes.get("user_id") == user_id]
    return UnlockInitiator(name, user_id, people[0] if len(people) == 1 else None, context)
