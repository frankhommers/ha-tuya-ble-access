"""Public access-list projection; never return PINs or protocol metadata."""

import time

from .credential_pause import CredentialPauseError, current_credential
from .temp_password_pause import TempPasswordPauseError, current_password


def access_items(store, lock_id, profile):
    now = time.time()
    services = profile.get("services", {})
    items = []
    for rec in store.get_credentials_for_lock(lock_id):
        start = end = None
        try:
            validity = bytes.fromhex((rec.device_policy or {}).get("validity_hex", ""))
            if len(validity) == 17:
                start, end = int.from_bytes(validity[:4], "big"), int.from_bytes(validity[4:8], "big")
        except (TypeError, ValueError):
            pass
        reason = None
        try:
            current_credential(store, lock_id, rec.credential_id, services.get("pause_credential", {}))
        except CredentialPauseError as err:
            reason = err.translation_key
        status = ("expired" if end is not None and end <= now else rec.pause_state)
        if status == "active" and start is not None and start > now:
            status = "scheduled"
        items.append({
            "id": rec.credential_id, "kind": {1: "pin", 2: "card", 3: "fingerprint"}.get(rec.cred_type, "other"),
            "name": rec.name, "person": store.credential_person(rec), "status": status,
            "finger": rec.finger,
            "effective_ts": start, "expiry_ts": end, "pause_reason": reason,
            "can_pause": reason is None and status != "paused",
            "can_resume": reason is None and status in ("paused", "unknown"),
            "needs_pin": rec.cred_type == 1 and not rec.pin_code,
            "can_delete": bool(rec.device_policy and rec.device_policy.get("admin") is False
                               and rec.device_policy.get("hw_id") == rec.hw_id
                               and rec.device_policy.get("cred_type") == rec.cred_type
                               and services.get("delete_credential", {}).get("dp") == 2),
        })
    # Show all retained temporary entries; lifecycle is visible, not a hidden filter.
    for data in store._data["temp_passwords"].values():
        if data["lock_entry_id"] != lock_id:
            continue
        reason = None
        cap = services.get("pause_temp_password", {})
        if cap.get("dp") != 53 or cap.get("strategy") != "no_weekdays":
            reason = "credential_pause_unsupported"
        else:
            try:
                current_password(store, lock_id, data["password_id"])
            except TempPasswordPauseError as err:
                reason = err.translation_key
        status = ("removed" if data.get("removed_at") is not None else
                  "replaced" if data.get("superseded_at") is not None else
                  "expired" if data["expiry_ts"] <= now else data.get("pause_state", "active"))
        if status == "active" and data["effective_ts"] > now:
            status = "scheduled"
        items.append({
            "id": data["password_id"], "kind": "temporary_pin", "name": data["name"],
            "person": data.get("person_entity_id"), "status": status,
            "effective_ts": data["effective_ts"], "expiry_ts": data["expiry_ts"],
            "pause_reason": reason, "can_pause": reason is None and status != "paused",
            "can_resume": reason is None and status in ("paused", "unknown"), "needs_pin": False,
            "can_delete": False,
        })
    return sorted(items, key=lambda row: (row["name"].casefold(), row["id"]))
