"""HA service context must not be confused with the lock's hardware user ID."""

import asyncio
import types
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_dp_parsing import _load
from test_temporary_passwords import coordinator
from test_credential_store import _fresh_store

attribution = _load('unlock_attribution')


def actor(name='Alice', user='ha-alice'):
    context = types.SimpleNamespace(id='context-' + name, parent_id=None, user_id=user)
    return attribution.UnlockInitiator(name, user, 'person.' + name.lower(), context)


def test_matching_is_one_shot_and_excludes_old_future_and_expired_reports():
    tracker = attribution.UnlockAttribution()
    request = tracker.start(actor(), 1000, 10)
    assert tracker.match(999, 11) is None
    assert tracker.match(1006, 11) is None
    assert tracker.match(1001, 12) is request.initiator
    assert tracker.match(1001, 13) is None
    tracker.start(actor('Bob'), 1010, 20)
    assert tracker.match(1011, 51) is None


def test_overlapping_and_replayed_commands_do_not_borrow_a_users_identity():
    tracker = attribution.UnlockAttribution()
    first = tracker.start(actor(), 1000, 10)
    assert tracker.match(1000, 10.1) is first.initiator
    tracker.start(actor('Bob'), 1001, 11)
    assert tracker.match(1001, 12) is None
    assert tracker.match(1000, 12) is None
    tracker = attribution.UnlockAttribution()
    tracker.start(actor(), 1000, 10)
    tracker.start(actor('Bob'), 1000, 10)
    assert tracker.match(1000, 11) is None


def test_cancellation_and_lock_isolation():
    first, second = attribution.UnlockAttribution(), attribution.UnlockAttribution()
    request = first.start(actor(), 1000, 10)
    assert second.match(1000, 11) is None
    first.cancel(request)
    assert first.match(1000, 11) is None


def setup(monkeypatch):
    c = coordinator(monkeypatch, _fresh_store())
    c._unlock_attribution = attribution.UnlockAttribution()
    c._op_lock = asyncio.Lock()
    c._device_data = {}
    c._session = types.SimpleNamespace(is_connected=True, async_send_dp_fire_and_forget=AsyncMock())
    c._async_ensure_connected = AsyncMock()
    c._fetch_status = AsyncMock()
    c._reset_idle_timer = Mock()
    c.hass.auth = types.SimpleNamespace(async_get_user=AsyncMock(return_value=types.SimpleNamespace(name='Alice')))
    c.hass.states = types.SimpleNamespace(async_all=Mock(return_value=[
        types.SimpleNamespace(entity_id='person.alice', attributes={'user_id': 'ha-alice'})]))
    time = c.async_unlock.__globals__['time']
    monkeypatch.setattr(time, 'time', lambda: 1000)
    monkeypatch.setattr(time, 'monotonic', lambda: 100)
    return c


def report(c, dp=19, timestamp=1000, snapshot=False):
    data = {'id': dp, 'type': 2, 'raw': b'\x00\x00\x00\x01'}
    if not snapshot:
        data['event_ts'] = timestamp
    c._process_dp_reports([data])


@pytest.mark.parametrize('motor_first', [False, True])
def test_command_context_follows_confirmed_bluetooth_event(monkeypatch, motor_first):
    async def run():
        c = setup(monkeypatch)
        context = actor().context
        async def sent(*args):
            if motor_first:
                c._process_dp_reports([{'id': 47, 'type': 1, 'raw': b'\x01'}])
            report(c)
        c._session.async_send_dp_fire_and_forget.side_effect = sent
        await c.async_unlock(context=context)
        assert c.state['last_unlock_by'] == 'Alice'
        assert c.state['last_unlock_person'] == 'person.alice'
        assert c.state['last_unlock_user'] == 1
        assert c.state['last_unlock_ha_user_id'] == 'ha-alice'
        assert c.state['last_unlock_context_id'] == context.id
        assert c.state['last_unlock_initiator_source'] == 'home_assistant'
        assert c.state['recent_unlocks'][0]['by'] == 'Alice'
        assert c.state['recent_unlocks'][0]['ha_user_id'] == 'ha-alice'
        events = [call for call in c.hass.bus.async_fire.call_args_list
                  if call.args[0] == 'tuya_ble_access_bluetooth_unlock']
        assert len(events) == 1
        assert events[0].kwargs['context'] is context
        report(c)
        assert len([call for call in c.hass.bus.async_fire.call_args_list
                    if call.args[0] == 'tuya_ble_access_bluetooth_unlock']) == 1
    asyncio.run(run())


def test_command_without_physical_report_does_not_claim_an_unlock(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        await c.async_unlock(context=actor().context)
        assert c.state.get('last_unlock_by') is None
        assert not c.state.get('recent_unlocks')
        c.hass.bus.async_fire.assert_not_called()
    asyncio.run(run())


def test_backlog_during_connection_and_status_snapshot_are_not_attributed(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        async def connected():
            report(c, timestamp=900)
        c._async_ensure_connected.side_effect = connected
        await c.async_unlock(context=actor().context)
        assert c.state['recent_unlocks'][0]['by'] == 'Bluetooth'
        report(c, snapshot=True)
        assert c.state.get('last_unlock_ha_user_id') is None
        report(c, timestamp=1001)
        assert c.state['last_unlock_by'] == 'Alice'
    asyncio.run(run())


@pytest.mark.parametrize('exception', [RuntimeError('write failed'), asyncio.CancelledError()])
def test_failed_or_cancelled_command_does_not_attribute_a_later_report(monkeypatch, exception):
    async def run():
        c = setup(monkeypatch)
        c._session.async_send_dp_fire_and_forget.side_effect = exception
        with pytest.raises(type(exception)):
            await c.async_unlock(context=actor().context)
        report(c)
        assert c.state['last_unlock_by'] == 'Bluetooth'
        assert c.state['last_unlock_ha_user_id'] is None
    asyncio.run(run())


def test_automation_without_user_stays_home_assistant(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        context = types.SimpleNamespace(id='automation-run', parent_id='trigger', user_id=None)
        await c.async_unlock(context=context)
        report(c)
        assert c.state['last_unlock_by'] == 'Home Assistant'
        assert c.state['last_unlock_ha_user_id'] is None
        assert c.state['last_unlock_parent_id'] == 'trigger'
        c.hass.auth.async_get_user.assert_not_awaited()
    asyncio.run(run())


def test_other_unlock_method_does_not_use_ha_identity(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        await c.async_unlock(context=actor().context)
        report(c, dp=12)
        assert c.state['last_unlock_by'] == 'User 1'
        assert c.state['last_unlock_ha_user_id'] is None
    asyncio.run(run())


def test_unknown_user_and_ambiguous_person_links_have_no_invented_person(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        c.hass.auth.async_get_user.return_value = None
        c.hass.states.async_all.return_value *= 2
        await c.async_unlock(context=actor().context)
        report(c)
        assert c.state['last_unlock_by'] == 'Home Assistant'
        assert c.state['last_unlock_person'] is None
        assert c.state['last_unlock_ha_user_id'] == 'ha-alice'
    asyncio.run(run())


def test_record_before_motor_preserves_initiator_once(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        await c.async_unlock(context=actor().context)
        report(c)
        c._process_dp_reports([{'id': 47, 'type': 1, 'raw': b'\x01'}])
        assert c.state['last_unlock_by'] == 'Alice'
        assert c.state['last_unlock_ha_user_id'] == 'ha-alice'
        c._process_dp_reports([{'id': 47, 'type': 1, 'raw': b'\x00'}])
        c._process_dp_reports([{'id': 47, 'type': 1, 'raw': b'\x01'}])
        assert c.state['last_unlock_by'] is None
        assert c.state['last_unlock_ha_user_id'] is None
    asyncio.run(run())
