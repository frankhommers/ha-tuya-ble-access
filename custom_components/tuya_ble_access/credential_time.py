"""Convert temporary PIN validity to UTC independently of the host timezone."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CredentialTimeError(ValueError):
    """A validity error with a localized service error key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.translation_key = key


def _timestamp(value: str, time_zone: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            zone = ZoneInfo(time_zone)
            # A wall time may occur zero, one, or twice across a DST transition.
            candidates = set()
            for fold in (0, 1):
                utc = parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                if utc.astimezone(zone).replace(tzinfo=None) == parsed:
                    candidates.add(utc)
            if not candidates:
                raise CredentialTimeError("temp_password_nonexistent_time")
            if len(candidates) > 1:
                raise CredentialTimeError("temp_password_ambiguous_time")
            parsed = candidates.pop()
        seconds = parsed.astimezone(UTC).timestamp()
        if not 0 <= seconds <= 0xFFFFFFFF:
            raise CredentialTimeError("temp_password_invalid_time")
        return int(seconds)
    except CredentialTimeError:
        raise
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError) as err:
        raise CredentialTimeError("temp_password_invalid_time") from err


def parse_credential_window(start: str, end: str, time_zone: str) -> tuple[int, int]:
    """Honor explicit offsets; interpret naive values in HA's configured zone."""
    effective = _timestamp(start, time_zone)
    expiry = _timestamp(end, time_zone)
    if expiry <= effective:
        raise CredentialTimeError("temp_password_invalid_period")
    return effective, expiry
