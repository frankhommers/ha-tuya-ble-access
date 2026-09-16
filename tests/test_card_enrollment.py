"""Card status handling must never invent a mapping for a duplicate response."""

import asyncio
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_activation_entrypoints import (
    FakeServiceHass, ServiceHomeAssistantError, _entry, services,
)
from test_enroll_payloads import ble_commands, const


@pytest.mark.parametrize("raw, error_key", [
    ("02fd0001ffff08", "card_already_enrolled"),
    ("02fd0001ffff06", "card_enrollment_failed"),
    ("02fe0001ffff08", "card_enrollment_failed"),
    ("02ff0001ff0100", "card_enrollment_unconfirmed"),
    ("02fd00", "card_timeout"),
])
def test_card_enrollment_does_not_save_failed_or_unassigned_slot(monkeypatch, raw, error_key):
    async def run():
        hass, handler, store, coord = await setup(monkeypatch, raw)
        with pytest.raises(ServiceHomeAssistantError) as caught:
            await handler(types.SimpleNamespace(data={"device_id": "MAC_A"}))
        assert caught.value.translation_key == error_key
        store.async_add_credential.assert_not_awaited()
        coord.async_update_listeners.assert_not_called()
        coord._session.async_disconnect.assert_awaited_once()
    asyncio.run(run())


def test_successful_card_enrollment_saves_real_slot_and_refreshes(monkeypatch):
    async def run():
        hass, handler, store, coord = await setup(monkeypatch, "02ff0001030100")
        # A delayed report for another credential type cannot fail this card request.
        coord._session.async_send_dp_raw_long.return_value.insert(
            0, {"id": 1, "type": 0, "raw": bytes.fromhex("03fd0001ffff08")}
        )
        await handler(types.SimpleNamespace(data={"device_id": "MAC_A"}))
        assert store.async_add_credential.await_args.kwargs["hw_id"] == 3
        assert store.async_add_credential.await_args.kwargs["cred_type"] == const.CRED_CARD
        coord.async_update_listeners.assert_called_once()
        coord._session.async_disconnect.assert_awaited_once()
    asyncio.run(run())


async def setup(monkeypatch, raw):
    monkeypatch.setattr(services, "parse_enroll_response", ble_commands.parse_enroll_response)
    monkeypatch.setattr(services, "build_enroll_payload", ble_commands.build_enroll_payload)
    monkeypatch.setattr(services, "CRED_CARD", const.CRED_CARD)
    coord = types.SimpleNamespace(
        profile={"services": {"add_card": {"dp": 1}}},
        _async_ensure_connected=AsyncMock(),
        _session=types.SimpleNamespace(
            async_send_dp_raw_long=AsyncMock(return_value=[{"id": 1, "type": 0, "raw": bytes.fromhex(raw)}]),
            async_disconnect=AsyncMock(),
        ),
        async_update_listeners=Mock(),
    )
    entry = _entry()
    entry.runtime_data = types.SimpleNamespace(coordinators={"MAC_A": coord})
    hass = FakeServiceHass([entry])
    store = types.SimpleNamespace(
        get_member_by_name=lambda name: types.SimpleNamespace(member_id=1),
        async_add_credential=AsyncMock(),
    )
    hass.data = {"tuya_ble_access": {"credential_store": store}}
    await services.async_register_services(hass)
    handler = hass.services.registration("tuya_ble_access", "add_card")[2]
    return hass, handler, store, coord
