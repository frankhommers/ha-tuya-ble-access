"""Dataclasses used by the Tuya BLE lock integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .coordinator import TuyaBLELockCoordinator
    from .credential_store import CredentialStore
    from .device_store import DeviceStore


@dataclass
class TuyaBLELockData:
    """Runtime data for the single hub config entry."""

    device_store: DeviceStore
    credential_store: CredentialStore
    coordinators: dict[str, TuyaBLELockCoordinator] = field(default_factory=dict)
    platforms: list = field(default_factory=list)
    # Callbacks that unsubscribe the advertisement watchers on unload.
    adv_unsubscribes: list = field(default_factory=list)


@dataclass
class MemberRecord:
    member_id: int
    name: str
    ha_user_id: Optional[str]
    created_at: float
    person_entity_id: Optional[str] = None  # e.g. "person.frank"


@dataclass
class CredentialRecord:
    credential_id: str
    member_id: int
    lock_entry_id: str
    cred_type: int
    hw_id: int
    name: str
    created_at: float


@dataclass
class TempPasswordRecord:
    password_id: str
    lock_entry_id: str
    name: str
    effective_ts: int
    expiry_ts: int
    created_at: float
    hw_id: Optional[int] = None
    superseded_at: Optional[float] = None
    removed_at: Optional[float] = None
