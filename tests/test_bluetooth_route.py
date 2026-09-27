"""Strict routes must never silently use another proxy."""
import asyncio
import importlib.util
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from test_i18n import ROOT, _load_select
from test_unlock_attribution import setup


def route_module(monkeypatch):
    monkeypatch.setitem(sys.modules, 'bleak.exc', NS(BleakError=RuntimeError))
    spec = importlib.util.spec_from_file_location('_route_test', ROOT / 'bluetooth_route.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, device, **kwargs):
        self.device = device
        self.kwargs = kwargs

    def _async_get_best_available_backend_and_device(self, manager):
        raise AssertionError('Automatic path selection must not run')

    def _async_get_backend_for_ble_device(self, manager, scanner, device):
        manager.selected.append(scanner.source)
        return scanner.backend


def manager(*sources):
    return NS(selected=[], async_scanner_devices_by_address=lambda *args: [
        NS(scanner=NS(source=source, backend=backend), ble_device=NS(address='lock'))
        for source, backend in sources
    ])


def test_automatic_retains_original_client(monkeypatch):
    assert route_module(monkeypatch).client_for_source(Client, None) is Client


def test_selected_source_wins_and_keeps_client_arguments(monkeypatch):
    cls = route_module(monkeypatch).client_for_source(Client, 'chosen')
    client = cls(NS(address='lock'), disconnected_callback='callback')
    m = manager(('stronger', 'wrong'), ('chosen', 'right'))
    assert client._async_get_best_available_backend_and_device(m) == 'right'
    assert m.selected == ['chosen']
    assert client.kwargs['disconnected_callback'] == 'callback'


@pytest.mark.parametrize('sources', [[], [('other', 'wrong')], [('chosen', None), ('other', 'wrong')]])
def test_missing_or_full_proxy_never_falls_back(monkeypatch, sources):
    client = route_module(monkeypatch).client_for_source(Client, 'chosen')(NS(address='lock'))
    m = manager(*sources)
    with pytest.raises(RuntimeError, match='Selected Bluetooth proxy'):
        client._async_get_best_available_backend_and_device(m)
    assert 'other' not in m.selected


def test_unsupported_ha_fails_closed(monkeypatch):
    with pytest.raises(RuntimeError, match='cannot enforce'):
        route_module(monkeypatch).client_for_source(object, 'chosen')


def test_selector_retains_offline_choice_and_can_return_to_automatic(monkeypatch):
    async def run():
        module = _load_select(monkeypatch)
        module.bluetooth.async_current_scanners = lambda _: [
            NS(name='Near', source='A', connectable=True),
            NS(name='Passive', source='B', connectable=False),
        ]
        c = NS(mac='lock', device_data={'bluetooth_source': 'offline'},
               async_set_bluetooth_source=AsyncMock())
        select = module.TuyaBLEBluetoothProxySelect(c, NS())
        select.hass = NS()
        select.async_write_ha_state = Mock()
        assert select.available
        assert select.current_option == 'offline'
        assert select.options == ['automatic', 'Near [A]', 'offline']
        await select.async_select_option('automatic')
        c.async_set_bluetooth_source.assert_awaited_once_with(None)
    asyncio.run(run())


def test_manual_refresh_bypasses_polling_cooldown_and_surfaces_failure(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        c._last_connect_failure = float('inf')
        await c.async_refresh_status_now()
        c._async_ensure_connected.assert_awaited_once()
        c._fetch_status.assert_awaited_once()
        c._async_ensure_connected.side_effect = RuntimeError('connection failed')
        with pytest.raises(RuntimeError, match='connection failed'):
            await c.async_refresh_status_now()
    asyncio.run(run())


def test_route_change_disconnects_then_persists_and_clears_cooldown(monkeypatch):
    async def run():
        c = setup(monkeypatch)
        order = []
        async def disconnect(): order.append('disconnect')
        async def save(*args, **kwargs): order.append('save')
        c._session.async_disconnect = disconnect
        c._entry.runtime_data.device_store = NS(async_update_device=save)
        c.async_update_listeners = Mock()
        await c.async_set_bluetooth_source('chosen')
        assert order == ['disconnect', 'save']
        assert c._session.bluetooth_source == 'chosen'
        assert c._device_data['bluetooth_source'] == 'chosen'
        assert c._last_connect_failure == 0
    asyncio.run(run())
