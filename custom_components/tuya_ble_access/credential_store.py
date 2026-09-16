"""Persistent credential/member database using Home Assistant Store."""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION, STORAGE_KEY
from .models import MemberRecord, CredentialRecord, TempPasswordRecord


# Shown when the lock says which member unlocked but not which credential of
# theirs was used -- naming a specific finger there would be a guess.
_GENERIC_CRED_LABEL = {1: "PIN", 2: "Card", 3: "Fingerprint", 4: "Face"}


class CredentialStore:
    def __init__(self, hass):
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data = None

    async def async_load(self) -> None:
        self._data = await self._store.async_load()
        if not self._data:
            self._data = {"version": STORAGE_VERSION, "members": {}, "credentials": {}, "temp_passwords": {}}

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    # Members
    def get_members(self) -> List[MemberRecord]:
        return [MemberRecord(**m) for m in self._data["members"].values()]

    def get_member(self, member_id: int) -> Optional[MemberRecord]:
        m = self._data["members"].get(str(member_id))
        return MemberRecord(**m) if m else None

    def get_member_by_name(self, name: str) -> Optional[MemberRecord]:
        for m in self.get_members():
            if m.name == name:
                return m
        return None

    async def async_add_member(
        self, name: str,
        ha_user_id: Optional[str] = None,
        person_entity_id: Optional[str] = None,
    ) -> MemberRecord:
        member_id = self.next_member_id()
        rec = MemberRecord(
            member_id=member_id, name=name, ha_user_id=ha_user_id,
            created_at=time.time(), person_entity_id=person_entity_id,
        )
        self._data["members"][str(member_id)] = rec.__dict__
        await self.async_save()
        return rec

    async def async_update_member(self, member_id: int, **kwargs) -> MemberRecord:
        rec = self.get_member(member_id)
        if not rec:
            raise KeyError("Member not found")
        for k, v in kwargs.items():
            setattr(rec, k, v)
        self._data["members"][str(member_id)] = rec.__dict__
        await self.async_save()
        return rec

    async def async_delete_member(self, member_id: int) -> None:
        self._data["members"].pop(str(member_id), None)
        # also remove credentials
        to_del = [cid for cid, c in self._data["credentials"].items() if c["member_id"] == member_id]
        for cid in to_del:
            self._data["credentials"].pop(cid, None)
        await self.async_save()

    def next_member_id(self) -> int:
        used = {int(k) for k in self._data["members"].keys()}
        for i in range(1, 101):
            if i not in used:
                return i
        raise RuntimeError("no member IDs available")

    # Credentials
    def get_credentials_for_lock(self, lock_entry_id: str) -> List[CredentialRecord]:
        return [CredentialRecord(**c) for c in self._data["credentials"].values() if c["lock_entry_id"] == lock_entry_id]

    def get_credentials_for_member(self, member_id: int) -> List[CredentialRecord]:
        return [CredentialRecord(**c) for c in self._data["credentials"].values() if c["member_id"] == member_id]

    def find_credential(self, lock_entry_id: str, cred_type: int, hw_id: int) -> Optional[CredentialRecord]:
        """Look up a credential by lock + credential type + hardware id.

        Used to resolve an incoming unlock event ('user 3 used a
        fingerprint') back to the HA member that owns that credential.
        """
        for c in self._data["credentials"].values():
            if (
                c["lock_entry_id"] == lock_entry_id
                and c["cred_type"] == cred_type
                and c["hw_id"] == hw_id
            ):
                return CredentialRecord(**c)
        return None

    async def async_add_credential(self, member_id, lock_entry_id, cred_type, hw_id, name) -> CredentialRecord:
        # One credential per physical slot: (lock, cred_type, hw_id) maps to a
        # single hardware slot on the lock, so re-enrolling that slot must
        # replace the old entry rather than stack a duplicate. Without this the
        # store fills with stale duplicates after re-pairs/re-enrolls and unlock
        # events resolve to the wrong (or ambiguous) member.
        existing = self.find_credential(lock_entry_id, cred_type, hw_id)
        if existing:
            self._data["credentials"].pop(existing.credential_id, None)
        cid = str(uuid.uuid4())
        rec = CredentialRecord(
            credential_id=cid,
            member_id=member_id,
            lock_entry_id=lock_entry_id,
            cred_type=cred_type,
            hw_id=hw_id,
            name=name,
            created_at=time.time(),
        )
        self._data["credentials"][cid] = rec.__dict__
        await self.async_save()
        return rec

    def resolve_unlock(self, lock_entry_id: str, cred_type: int, value: int):
        """Resolve an unlock event's id to (member, credential_name).

        Verified on a K3 BLE PRO 2 (2026-09-01): the unlock DP carries the
        *credential slot* (hw_id) -- unlocking with slot 1 reports 1 and slot 2
        reports 2 -- so the specific finger is knowable. Slot lookup therefore
        wins; a member id that happens to equal a slot number must not hijack it.

        Falls back to reading the value as a member id, which yields the person
        but only a generic label ("Fingerprint"), since in that case the event
        does not identify which of their credentials was used.
        """
        cred = self.find_credential(lock_entry_id, cred_type, value)
        if cred:
            return self.get_member(cred.member_id), cred.name
        member = self.get_member(value)
        if member:
            creds = [
                c for c in self.get_credentials_for_member(member.member_id)
                if c.lock_entry_id == lock_entry_id and c.cred_type == cred_type
            ]
            if creds:
                return member, _GENERIC_CRED_LABEL.get(cred_type)
        return None, None

    async def async_report_factory_reset(self, lock_entry_id: str) -> dict:
        """Forget one reset lock's credentials, retaining shared members.

        This records a reset already performed on the hardware. It does not
        contact the lock or modify its Bluetooth pairing credentials.
        """
        if not lock_entry_id:
            raise ValueError("A lock is required to report a factory reset")
        previous = self._data
        updated = dict(previous)
        removed = {}
        for key in ("credentials", "temp_passwords"):
            updated[key] = {
                record_id: record
                for record_id, record in previous[key].items()
                if record["lock_entry_id"] != lock_entry_id
            }
            removed[key] = len(previous[key]) - len(updated[key])
        self._data = updated
        try:
            await self.async_save()
        except BaseException:
            self._data = previous
            raise
        return removed

    async def async_clear(self, lock_entry_id: Optional[str] = None) -> int:
        """Drop attribution metadata. With a lock id, only that lock's
        credentials; without one, every credential and member.

        Returns the number of credentials removed. This only clears HA-side
        metadata — it never removes fingerprints from the lock hardware.
        """
        creds = self._data["credentials"]
        if lock_entry_id is None:
            removed = len(creds)
            self._data["credentials"] = {}
            self._data["members"] = {}
        else:
            to_del = [
                cid for cid, c in creds.items()
                if c["lock_entry_id"] == lock_entry_id
            ]
            for cid in to_del:
                creds.pop(cid, None)
            removed = len(to_del)
        await self.async_save()
        return removed

    async def async_delete_credential(self, credential_id: str) -> Optional[CredentialRecord]:
        rec = self._data["credentials"].pop(credential_id, None)
        if rec:
            await self.async_save()
            return CredentialRecord(**rec)
        return None

    # Temp passwords
    def get_temp_passwords_for_lock(self, lock_entry_id) -> list[TempPasswordRecord]:
        return [TempPasswordRecord(**value) for value in self._data["temp_passwords"].values()
                if value["lock_entry_id"] == lock_entry_id and value.get("superseded_at") is None
                and value.get("removed_at") is None]

    def get_temp_password_overview(self, lock_entry_id) -> dict:
        """Separate usable/pending codes from expired cleanup and the archive."""
        now = time.time()
        active, expired = [], []
        for rec in self.get_temp_passwords_for_lock(lock_entry_id):
            row = {"password_id": rec.password_id, "name": rec.name, "hw_id": rec.hw_id,
                   "effective_ts": rec.effective_ts, "expiry_ts": rec.expiry_ts}
            (expired if rec.expiry_ts <= now else active).append(row)
        archived = sum(1 for rec in self._data["temp_passwords"].values()
                       if rec["lock_entry_id"] == lock_entry_id
                       and (rec.get("removed_at") is not None or rec.get("superseded_at") is not None))
        return {"temporary_passwords": active, "expired_temporary_passwords": expired,
                "archived_temporary_password_count": archived}

    def get_expired_temp_passwords(self, lock_entry_id, now) -> list[TempPasswordRecord]:
        current = self.get_temp_passwords_for_lock(lock_entry_id)
        return [rec for rec in current
                if rec.expiry_ts <= now and type(rec.hw_id) is int and 0 <= rec.hw_id <= 254
                and sum(other.hw_id == rec.hw_id for other in current) == 1]

    async def async_archive_temp_password(self, password_id, removed_at) -> bool:
        """Archive a confirmed deletion, retaining its historical identity."""
        current = self._data["temp_passwords"].get(password_id)
        if current is None or current.get("superseded_at") is not None or current.get("removed_at") is not None:
            return False
        updated = {**current, "removed_at": removed_at}
        self._data["temp_passwords"][password_id] = updated
        try:
            await self.async_save()
        except BaseException:
            if self._data["temp_passwords"].get(password_id) is updated:
                self._data["temp_passwords"][password_id] = current
            raise
        return True

    async def async_add_temp_password(self, lock_entry_id, name, effective, expiry, hw_id=None) -> TempPasswordRecord:
        if hw_id is not None and (type(hw_id) is not int or not 0 <= hw_id <= 254):
            raise ValueError("Invalid temporary PIN hardware ID")
        pid = str(uuid.uuid4())
        rec = TempPasswordRecord(
            password_id=pid,
            lock_entry_id=lock_entry_id,
            name=name,
            effective_ts=effective,
            expiry_ts=expiry,
            created_at=time.time(),
            hw_id=hw_id,
        )
        previous = self._data["temp_passwords"]
        updated = {key: dict(value) for key, value in previous.items()}
        if hw_id is not None:
            for old in updated.values():
                if (old["lock_entry_id"] == lock_entry_id and old.get("hw_id") == hw_id
                        and old.get("superseded_at") is None):
                    old["superseded_at"] = rec.created_at
        updated[pid] = rec.__dict__
        self._data["temp_passwords"] = updated
        try:
            await self.async_save()
        except BaseException:
            self._data["temp_passwords"] = previous
            raise
        return rec

    def resolve_temp_password(self, lock_entry_id, hw_id, event_ts) -> Optional[TempPasswordRecord]:
        """Resolve an exact device ID; never guess from a validity period.

        Keep earlier generations for delayed records when the lock reuses an ID.
        Records with an unreliable timestamp or overlapping generations stay unknown.
        """
        candidates = [value for value in self._data["temp_passwords"].values()
                      if value["lock_entry_id"] == lock_entry_id
                      and value.get("hw_id") is not None and value["hw_id"] == hw_id
                      and int(value["created_at"]) <= event_ts
                      and (value.get("superseded_at") is None
                           or event_ts < value["superseded_at"])
                      and (value.get("removed_at") is None or event_ts < value["removed_at"])]
        return TempPasswordRecord(**candidates[0]) if len(candidates) == 1 else None

    async def async_delete_temp_password(self, password_id: str) -> None:
        self._data["temp_passwords"].pop(password_id, None)
        await self.async_save()
