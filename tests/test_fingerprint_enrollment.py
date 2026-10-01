"""Keep finger metadata independent of labels; never save failed enrollment."""
import asyncio
import types

import pytest

from test_card_enrollment import setup
from test_activation_entrypoints import ServiceHomeAssistantError


@pytest.mark.parametrize('success', [True, False])
def test_finger_choice_saved_only_after_success(monkeypatch, success):
    async def run():
        raw = '03ff0001010600' if success else '03fd000101ff01'
        hass, _, store, coord = await setup(monkeypatch, raw)
        coord.profile = {'services': {'add_fingerprint': {'dp': 1}}}
        handler = hass.services.registration('tuya_ble_access', 'add_fingerprint')[2]
        call = types.SimpleNamespace(data={
            'device_id': 'MAC_A', 'finger': 'right_index', 'name': 'My custom label',
        })
        if success:
            await handler(call)
            saved = store.async_add_credential.await_args.kwargs
            assert saved['finger'] == 'right_index'
            assert saved['name'] == 'My custom label'
        else:
            with pytest.raises(ServiceHomeAssistantError) as caught:
                await handler(call)
            assert caught.value.translation_key == 'fingerprint_enrollment_failed'
            assert caught.value.translation_placeholders == {'code': '0x01'}
            store.async_add_credential.assert_not_awaited()
    asyncio.run(run())
