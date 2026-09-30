"""Pause a verified credential while retaining its exact enrollment policy."""

import re
import time

from .credential_schedule import build_schedule_probe, schedule_probe_succeeded


class CredentialPauseError(ValueError):
    def __init__(self, key):
        self.translation_key = key
        super().__init__(key)


def current_credential(store, lock_id, credential_id, capability):
    records = store.get_credentials_for_lock(lock_id)
    rec = next((r for r in records if r.credential_id == credential_id), None)
    if rec is None or sum((r.cred_type, r.hw_id) == (rec.cred_type, rec.hw_id) for r in records) != 1:
        raise CredentialPauseError("credential_not_current")
    if (capability.get("dp") != 3 or capability.get("strategy") != "no_weekdays"
            or rec.cred_type not in capability.get("credential_types", [])):
        raise CredentialPauseError("credential_pause_unsupported")
    policy = rec.device_policy
    if (not isinstance(policy, dict) or policy.get("cred_type") != rec.cred_type
            or policy.get("hw_id") != rec.hw_id):
        raise CredentialPauseError("credential_policy_unknown")
    try:
        restore = build_schedule_probe(policy, paused=False)
    except (ValueError, TypeError):
        raise CredentialPauseError("credential_policy_unknown") from None
    if int.from_bytes(restore[9:13], "big") <= time.time():
        raise CredentialPauseError("credential_expired")
    return rec


def credential_pause_pin(rec, pin_code=None):
    """Use a remembered PIN; supplied digits must not silently replace it."""
    if rec.cred_type == 1:
        if pin_code in (None, ""):
            pin_code = rec.pin_code
        if not isinstance(pin_code, str) or not re.fullmatch(r"[0-9]{6,10}", pin_code):
            raise CredentialPauseError("credential_pin_required")
        if rec.pin_code is not None and pin_code != rec.pin_code:
            raise CredentialPauseError("credential_pin_mismatch")
        return pin_code
    elif pin_code not in (None, ""):
        raise CredentialPauseError("credential_pin_not_applicable")
    return None


def credential_pause_payload(rec, paused, pin_code=None):
    """Validate PIN input before connecting or changing stored state."""
    return build_schedule_probe(
        rec.device_policy, paused=paused, pin_code=credential_pause_pin(rec, pin_code)
    )


async def async_set_credential_paused(
    store, session, lock_id, credential_id, capability, paused, *, pin_code=None
):
    """Caller serializes credential services and holds coordinator operation lock."""
    rec = current_credential(store, lock_id, credential_id, capability)
    payload = credential_pause_payload(rec, paused, pin_code)
    # Remember before BLE so a timeout/restart can still be recovered by resume.
    await store.async_set_credential_pause_state(
        rec, "unknown", paused, pin_code=credential_pause_pin(rec, pin_code)
    )
    fresh = current_credential(store, lock_id, credential_id, capability)
    if fresh.device_policy != rec.device_policy:
        raise CredentialPauseError("credential_not_current")
    response = await session.async_send_dp_raw(3, payload)
    if not schedule_probe_succeeded(response, payload):
        raise CredentialPauseError("credential_pause_unconfirmed")
    fresh = current_credential(store, lock_id, credential_id, capability)
    if fresh.device_policy != rec.device_policy:
        raise CredentialPauseError("credential_not_current")
    state = "paused" if paused else "active"
    await store.async_set_credential_pause_state(rec, state, paused)
    return {"credential_id": rec.credential_id, "name": rec.name, "pause_state": state}
