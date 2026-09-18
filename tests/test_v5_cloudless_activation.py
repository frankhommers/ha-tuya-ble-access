"""Offline tests for V5 cloudless activation helpers."""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _load_package_module(name: str):
    pkg = sys.modules.setdefault("_tblpkg", types.ModuleType("_tblpkg"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblpkg.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(
        fq, ROOT / f"{name}.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    return mod


def _install_homeassistant_stubs():
    ha = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = types.ModuleType("homeassistant.helpers")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda _hass: None
    ha.core = core
    ha.helpers = helpers
    helpers.aiohttp_client = aiohttp_client
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client


def _install_ble_stubs():
    bleak = types.ModuleType("bleak")
    bleak.BleakClient = object
    retry = types.ModuleType("bleak_retry_connector")
    retry.establish_connection = None
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    bluetooth.async_ble_device_from_address = lambda *_args, **_kwargs: None
    components.bluetooth = bluetooth
    sys.modules["bleak"] = bleak
    sys.modules["bleak_retry_connector"] = retry
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.bluetooth"] = bluetooth


def test_verify_key_constant_is_defined():
    const = _load_package_module("const")

    assert const.CONF_VERIFY_KEY == "verify_key"


def test_cloud_sign_field_normalizes_to_verify_key():
    _install_homeassistant_stubs()
    tuya_cloud = _load_package_module("tuya_cloud")

    assert tuya_cloud._extract_verify_key({"sign": "440260d0"}) == "440260d0"
    assert tuya_cloud._extract_verify_key({"verifyKey": "aabbccdd"}) == "aabbccdd"


def test_cloud_auth_response_exposes_replay_random():
    _install_homeassistant_stubs()
    tuya_cloud = _load_package_module("tuya_cloud")

    assert tuya_cloud._extract_auth_random({"random": "001122"}) == "001122"
    assert tuya_cloud._extract_auth_random({"randomKey": "aabbcc"}) == "aabbcc"


def test_v5_pair_payload_includes_verify_key_and_struct_dp():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    payload = ble_session._build_v5_pair_payload(
        local_key=b"test-local-key01",
        sec_key=b"test-sec-key0001",
        dev_id="bfd1faekehdxvras",
        dev_uuid="uuidc064f275f947",
        verify_key=bytes.fromhex("440260d0"),
        need_beacon=False,
        support_struct_dp=False,
    )
    payload_struct = ble_session._build_v5_pair_payload(
        local_key=b"test-local-key01",
        sec_key=b"test-sec-key0001",
        dev_id="bfd1faekehdxvras",
        dev_uuid="uuidc064f275f947",
        verify_key=bytes.fromhex("440260d0"),
        need_beacon=False,
        support_struct_dp=True,
    )

    assert len(payload) == 85
    assert payload[-7:] == bytes.fromhex("440260d0000000")
    assert len(payload_struct) == 86
    assert payload_struct[-8:] == bytes.fromhex("440260d001010000")


def test_pairing_connection_resolves_fresh_ble_device_each_attempt():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    async def run_test():
        class FakeClient:
            is_connected = True

            async def stop_notify(self, _uuid):
                raise RuntimeError("No active notification")

            async def start_notify(self, _uuid, _callback):
                pass

            async def disconnect(self):
                self.is_connected = False

        hass = object()
        address = "AA:BB:CC:DD:EE:FF"
        original_device = types.SimpleNamespace(address=address)
        fresh_devices = [
            types.SimpleNamespace(
                address=address, details={"source": "esphome-proxy-1"}, rssi=-61
            ),
            types.SimpleNamespace(
                address=address, details={"source": "esphome-proxy-2"}, rssi=-58
            ),
        ]
        lookup_calls = []
        connection_devices = []

        def fake_ble_lookup(lookup_hass, lookup_address, *, connectable):
            lookup_calls.append((lookup_hass, lookup_address, connectable))
            return fresh_devices[len(lookup_calls) - 1]

        async def fake_establish_connection(**kwargs):
            connection_devices.append(kwargs["device"])
            return FakeClient()

        session = ble_session.TuyaBLELockSession(
            hass=hass,
            ble_device=original_device,
            login_key=b"",
            virtual_id=b"",
            device_uuid="uuidc064f275f947",
        )
        session._resolve_gatt_uuids = lambda: ("write", "notify")

        original_ble_lookup = ble_session.bluetooth.async_ble_device_from_address
        original_establish_connection = ble_session.establish_connection
        ble_session.bluetooth.async_ble_device_from_address = fake_ble_lookup
        ble_session.establish_connection = fake_establish_connection
        try:
            assert await session._connect_for_pairing()
            assert await session._connect_for_pairing()
        finally:
            ble_session.bluetooth.async_ble_device_from_address = original_ble_lookup
            ble_session.establish_connection = original_establish_connection

        assert lookup_calls == [
            (hass, address, True),
            (hass, address, True),
        ]
        assert connection_devices == fresh_devices
        assert session._ble_device is fresh_devices[-1]

    asyncio.run(run_test())


def _new_v5_session(ble_session):
    return ble_session.TuyaBLELockSession(
        hass=object(),
        ble_device=types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        login_key=b"abcdef",
        virtual_id=(b"device-id" + b"\x00" * 22)[:22],
        device_uuid="uuidc064f275f947",
        auth_key=bytes.fromhex("00" * 16),
        auth_random=bytes.fromhex("11" * 16),
        local_key=b"abcdefghijklmnop",
        sec_key=b"ponmlkjihgfedcba",
        verify_key=bytes.fromhex("aabbccdd"),
    )


def test_v5_first_activation_reports_already_bound_with_typed_error():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    async def run_test():
        session = _new_v5_session(ble_session)

        async def connected():
            return True

        async def bound_info(*_args, **_kwargs):
            return {
                "is_bound": True,
                "srand": b"123456",
                "need_beacon": False,
                "support_struct_dp": False,
            }

        session._connect_for_pairing = connected
        session._request_v5_device_info = bound_info

        try:
            await session._async_pair_first_activation_v5()
        except Exception as exc:
            assert type(exc).__name__ == "DeviceAlreadyBoundError"
        else:
            raise AssertionError("already-bound lock was accepted for activation")

    asyncio.run(run_test())


def test_v5_first_activation_reports_pair_rejection_with_typed_error():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    async def run_test():
        session = _new_v5_session(ble_session)
        session._client = types.SimpleNamespace(is_connected=True)

        async def unbound_info(*_args, **_kwargs):
            return {
                "is_bound": False,
                "srand": b"123456",
                "need_beacon": False,
                "support_struct_dp": False,
            }

        async def rejected_pair(*_args, **_kwargs):
            return [{"cmd": ble_session.CMD_PAIR, "data": b"\x01"}]

        async def connected():
            return True

        async def not_bound():
            return False

        session._connect_for_pairing = connected
        session._request_v5_device_info = unbound_info
        session._send_recv = rejected_pair
        session._verify_v5_bound = not_bound

        try:
            await session._async_pair_first_activation_v5()
        except Exception as exc:
            assert type(exc).__name__ == "PairingFailedError"
        else:
            raise AssertionError("rejected PAIR was accepted")

    asyncio.run(run_test())


def test_v5_first_activation_reports_bind_verification_with_typed_error():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    async def run_test():
        session = _new_v5_session(ble_session)
        session._client = types.SimpleNamespace(is_connected=True)

        async def unbound_info(*_args, **_kwargs):
            return {
                "is_bound": False,
                "srand": b"123456",
                "need_beacon": False,
                "support_struct_dp": False,
            }

        async def accepted_pair(*_args, **_kwargs):
            return [{"cmd": ble_session.CMD_PAIR, "data": b"\x00"}]

        async def connected():
            return True

        async def not_bound():
            return False

        session._connect_for_pairing = connected
        session._request_v5_device_info = unbound_info
        session._send_recv = accepted_pair
        session._verify_v5_bound = not_bound

        try:
            await session._async_pair_first_activation_v5()
        except Exception as exc:
            assert type(exc).__name__ == "BindVerificationError"
        else:
            raise AssertionError("unverified bind was accepted")

    asyncio.run(run_test())


def test_v5_first_activation_uses_sec11_then_sec12():
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")

    async def run_test():
        class FakeClient:
            is_connected = True
            services = []

            async def stop_notify(self, _uuid):
                pass

            async def start_notify(self, _uuid, _callback):
                pass

        async def fake_establish_connection(**_kwargs):
            return FakeClient()

        async def fake_sleep(_seconds):
            pass

        calls = []
        srand = bytes.fromhex("112233445566")
        device_info = bytes([0, 0, 5, 0, 0, 0]) + srand + bytes(76) + b"\x08\x00\x00"

        def fake_parse_frames(_keys, raw):
            if raw == [b"device_info"]:
                return [{"cmd": ble_session.CMD_DEVICE_INFO, "sn": 1, "ack_sn": 0, "data": device_info, "sec_flag": 11}]
            return []

        session = ble_session.TuyaBLELockSession(
            hass=object(),
            ble_device=types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
            login_key=b"",
            virtual_id=b"bfd1faekehdxvras\x00\x00\x00\x00\x00\x00",
            device_uuid="uuidc064f275f947",
            auth_key=bytes.fromhex("00" * 16),
            auth_random=bytes.fromhex("11" * 16),
            local_key=b"test-local-key01",
            sec_key=b"test-sec-key0001",
            verify_key=bytes.fromhex("440260d0"),
        )
        session._resolve_gatt_uuids = lambda: ("write", "notify")

        async def fake_send(cmd, data, sec_flag, ack_sn=0, fixed_iv=None):
            calls.append((cmd, data, sec_flag, ack_sn))
            if cmd == ble_session.CMD_DEVICE_INFO:
                session._notif_buf.append(b"device_info")

        original_establish_connection = ble_session.establish_connection
        original_sleep = asyncio.sleep
        original_parse_frames = ble_session.parse_frames
        ble_session.establish_connection = fake_establish_connection
        ble_session.asyncio.sleep = fake_sleep
        ble_session.parse_frames = fake_parse_frames
        session._send_encrypted = fake_send

        try:
            try:
                await session.async_pair_first_activation("00" * 16)
            except Exception:
                pass
        finally:
            ble_session.establish_connection = original_establish_connection
            ble_session.asyncio.sleep = original_sleep
            ble_session.parse_frames = original_parse_frames

        assert calls[0][0] == ble_session.CMD_DEVICE_INFO
        assert calls[0][2] == 11
        pair_calls = [call for call in calls if call[0] == ble_session.CMD_PAIR]
        assert pair_calls
        assert pair_calls[0][2] == 12
        assert bytes.fromhex("440260d0") in pair_calls[0][1]

    asyncio.run(run_test())


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures.append((name, exc))
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:
            failures.append((name, exc))
            print(f"ERROR {name}: {exc!r}")
    sys.exit(1 if failures else 0)


def test_bluetooth_device_info_deadline_does_not_send_pair(monkeypatch):
    _install_homeassistant_stubs()
    _install_ble_stubs()
    ble_session = _load_package_module("ble_session")
    real_timeout = asyncio.timeout

    async def run_test():
        session = _new_v5_session(ble_session)
        cancelled = asyncio.Event()
        deadlines = []

        async def stalled_connect():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def unexpected_pair(*_args, **_kwargs):
            raise AssertionError("A device-info timeout must never send PAIR")

        def short_timeout(seconds):
            deadlines.append(seconds)
            return real_timeout(0.01)

        monkeypatch.setattr(ble_session.asyncio, "timeout", short_timeout)
        session._connect_for_pairing = stalled_connect
        session._send_recv = unexpected_pair
        try:
            await session._async_pair_first_activation_v5()
        except ble_session.PairingFailedError as exc:
            assert "no PAIR command was sent" in str(exc)
        else:
            raise AssertionError("The Bluetooth deadline was not enforced")
        assert deadlines == [45]
        assert cancelled.is_set()

    asyncio.run(run_test())


def test_unknown_account_device_does_not_fetch_authentication_key(monkeypatch):
    _install_homeassistant_stubs()
    tuya_cloud = _load_package_module("tuya_cloud")
    calls = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def async_login(self, *_args):
            calls.append("login")
            return {"success": True}

        async def async_find_device_by_mac(self, _mac):
            calls.append("lookup")
            return None

        async def async_get_ble_auth_key(self, *_args, **_kwargs):
            raise AssertionError("Unknown account device must not fetch an auth key")

    monkeypatch.setattr(tuya_cloud, "TuyaMobileAPIAsync", Client)
    result = asyncio.run(tuya_cloud.async_fetch_auth_key(
        object(), "advertised-uuid", "user@example.com", "password", "31", "eu",
        device_mac="AA:BB:CC:DD:EE:FF",
    ))
    assert result == {"uuid": "advertised-uuid", "device_id": ""}
    assert calls == ["login", "lookup"]


def test_failed_account_lookup_is_not_treated_as_an_empty_account(monkeypatch):
    _install_homeassistant_stubs()
    tuya_cloud = _load_package_module("tuya_cloud")

    async def run_test():
        client = tuya_cloud.TuyaMobileAPIAsync(None)

        async def homes():
            return {"success": True, "result": [{"groupId": 1}]}

        async def failed(*_args):
            return {"success": False}

        monkeypatch.setattr(client, "async_get_home_list", failed)
        try:
            await client.async_find_device_by_mac("AA:BB:CC:DD:EE:FF")
        except RuntimeError as exc:
            assert str(exc) == "Could not list Tuya homes"
        else:
            raise AssertionError("Cloud error became an unknown lock")
        monkeypatch.setattr(client, "async_get_home_list", homes)
        monkeypatch.setattr(client, "async_list_devices", failed)
        try:
            await client.async_find_device_by_mac("AA:BB:CC:DD:EE:FF")
        except RuntimeError as exc:
            assert str(exc) == "Could not list Tuya devices"
        else:
            raise AssertionError("Cloud error became an unknown lock")

    asyncio.run(run_test())
