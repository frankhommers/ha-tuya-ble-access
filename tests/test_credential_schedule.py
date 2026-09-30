"""DP3 candidates must preserve exact identity and restoration policy."""
import asyncio
import copy

import pytest

from test_credential_store import _fresh_store, _load

schedule = _load('credential_schedule')
commands = _load('ble_commands')


def policy(kind=2):
    payload = commands.build_enroll_payload(kind, 7, password_digits=[1]*6 if kind == 1 else None)
    response = {'id': 1, 'type': 0, 'raw': bytes([kind, 255, 0, 7, 9, 0, 0])}
    return schedule.policy_from_enrollment(payload, 1, response)


@pytest.mark.parametrize('kind', [1, 2, 3])
def test_exact_pause_and_restore_without_secret(kind):
    original = policy(kind)
    before = copy.deepcopy(original)
    paused = schedule.build_schedule_probe(original, paused=True)
    restored = schedule.build_schedule_probe(original, paused=False)
    assert paused[:5] == restored[:5] == bytes([kind, 0, 0, 7, 9])
    assert paused[5:13] == restored[5:13]
    assert paused[13:22] == schedule.NO_WEEKDAYS
    assert restored[5:22] == commands.build_validity_permanent()
    assert len(paused) == len(restored) == 24
    assert paused[22:] == restored[22:] == bytes(2)
    assert original == before
    assert set(original) == {'source', 'cred_type', 'member_id', 'hw_id', 'admin', 'validity_hex', 'uses'}


@pytest.mark.parametrize('field,value', [('source', None), ('member_id', None), ('member_id', 255),
    ('member_id', True), ('admin', True), ('admin', None), ('hw_id', 255), ('cred_type', 0),
    ('uses', 1), ('validity_hex', ''), ('validity_hex', '00'*17)])
def test_unknown_policy_cannot_be_guessed(field, value):
    p = policy(); p[field] = value
    with pytest.raises(ValueError):
        schedule.build_schedule_probe(p, paused=True)


def test_member_scope_never_inferred_from_credential():
    p = policy()
    with pytest.raises(ValueError, match='not a member'):
        schedule.build_schedule_probe(p, paused=True, scope='member')
    p['source'] = 'confirmed_member_schedule'
    result = schedule.build_schedule_probe(p, paused=False, scope='member')
    assert result == bytes([0, 0, 0, 7, 255]) + commands.build_validity_permanent() + bytes(3)


@pytest.mark.parametrize('pin', ['001234', '0123456789'])
def test_pin_schedule_preserves_digits_and_encodes_length(pin):
    original = policy(1)
    before = copy.deepcopy(original)
    for paused in [True, False]:
        payload = schedule.build_schedule_probe(original, paused=paused, pin_code=pin)
        assert payload[:5] == bytes([1, 0, 0, 7, 9])
        assert payload[5:13] == bytes.fromhex(original['validity_hex'])[:8]
        assert payload[13:22] == (schedule.NO_WEEKDAYS if paused else bytes.fromhex(original['validity_hex'])[8:])
        assert payload[22:] == bytes([0, len(pin)]) + bytes(int(d) for d in pin)
    assert original == before


@pytest.mark.parametrize('pin', ['', '12345', '12345678901', '１２３４５６', '12345x', 123456])
def test_invalid_pin_content_is_rejected_without_echo(pin):
    with pytest.raises(ValueError) as err:
        schedule.build_schedule_probe(policy(1), paused=True, pin_code=pin)
    if pin:
        assert str(pin) not in str(err.value)


@pytest.mark.parametrize('kind', [2, 3])
def test_card_and_fingerprint_never_receive_pin_content(kind):
    with pytest.raises(ValueError):
        schedule.build_schedule_probe(policy(kind), paused=True, pin_code='001234')


@pytest.mark.parametrize('offset,value', [(0, 3), (1, 252), (2, 1), (3, 8), (4, 255), (6, 1)])
def test_mismatched_or_incomplete_enrollment_is_unknown(offset, value):
    raw = bytearray([2, 255, 0, 7, 9, 0, 0]); raw[offset] = value
    assert schedule.policy_from_enrollment(commands.build_enroll_payload(2, 7), 1,
        {'id': 1, 'type': 0, 'raw': bytes(raw)}) is None


def test_matching_ack_required_and_dp3_success_is_ff():
    payload = schedule.build_schedule_probe(policy(), paused=True)
    raw = payload[:5] + b'\x00\xff'
    assert schedule.schedule_probe_succeeded({'id': 3, 'type': 0, 'raw': raw}, payload)
    for reply in [None, {'id': 53, 'type': 0, 'raw': raw}, {'id': 3, 'type': 1, 'raw': raw},
                  {'id': 3, 'type': 0, 'raw': raw[:-1]+b'\x00'},
                  {'id': 3, 'type': 0, 'raw': raw[:4]+b'\x08'+raw[5:]}]:
        assert not schedule.schedule_probe_succeeded(reply, payload)


def test_persist_policy_but_never_infer_for_legacy_or_reregistered_slot():
    async def run():
        store = _fresh_store()
        rec = await store.async_add_credential(7, 'LOCK', 2, 9, 'Test', device_policy=policy())
        restarted = _fresh_store(); restarted._data = copy.deepcopy(store._data)
        assert restarted.find_credential('LOCK', 2, 9).device_policy == policy()
        # Local re-attribution is not a device enrollment or permission update.
        replacement = await restarted.async_add_credential(8, 'LOCK', 2, 9, 'Renamed')
        assert replacement.device_policy is None
        del restarted._data['credentials'][replacement.credential_id]['device_policy']
        assert restarted.get_credentials_for_lock('LOCK')[0].device_policy is None
    asyncio.run(run())


def test_offline_probe_plan_and_exact_restore():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('probe_plan', Path(__file__).resolve().parents[1] / 'scripts/prepare_credential_pause_probe.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    row = {'credential_id': 'test', 'name': 'Pauzetest PIN', 'hw_id': 9,
           'type': 'pin', 'device_policy': policy(1)}
    plan = module.prepare({'credentials': [row]}, 'test', 'device', now=1790544600)
    restore = bytes.fromhex(plan['restore_action']['data']['payloads'][0])
    assert restore[5:22] == commands.build_validity_permanent()
    assert plan['expected_ack_hex'] == '010000070900ff'
    for change in [{'name': 'Real access'}, {'device_policy': None}, {'hw_id': 8}, {'type': 'card'}]:
        with pytest.raises(ValueError):
            module.prepare({'credentials': [{**row, **change}]}, 'test', 'device', now=1790544600)
    with pytest.raises(ValueError):
        module.prepare({'credentials': [row]}, 'test', 'device', now=2000000000)
