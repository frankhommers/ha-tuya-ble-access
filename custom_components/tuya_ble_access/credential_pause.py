"""Pause a verified credential while retaining its exact enrollment policy."""

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


async def async_set_credential_paused(store, session, lock_id, credential_id, capability, paused):
    """Caller serializes credential services and holds coordinator operation lock."""
    rec = current_credential(store, lock_id, credential_id, capability)
    payload = build_schedule_probe(rec.device_policy, paused=paused)
    await store.async_set_credential_pause_state(rec, "unknown", paused)
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
