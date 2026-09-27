"""Optional strict connection routing without changing HA's global manager.

Passing a scanner's BLEDevice to BleakClient does NOT pin a connection: HA
resolves the address again. Override only its path selection for this client,
retaining HA's slot accounting, connect/disconnect and failure handling.
These protected hooks are compatibility checked; an unsupported HA fails
closed when a route is configured. Automatic routing uses the original class.
"""

from bleak.exc import BleakError


def client_for_source(client_class, source: str | None):
    if not source:
        return client_class
    if not all(callable(getattr(client_class, name, None)) for name in (
        "_async_get_best_available_backend_and_device",
        "_async_get_backend_for_ble_device",
    )):
        raise BleakError("This Home Assistant version cannot enforce a fixed Bluetooth proxy")

    class RoutedClient(client_class):
        def __init__(self, device, *args, **kwargs):
            self._route_address = device.address
            super().__init__(device, *args, **kwargs)

        def _async_get_best_available_backend_and_device(self, manager):
            for candidate in manager.async_scanner_devices_by_address(self._route_address, True):
                if candidate.scanner.source != source:
                    continue
                backend = self._async_get_backend_for_ble_device(
                    manager, candidate.scanner, candidate.ble_device
                )
                if backend is not None:
                    return backend
                raise BleakError(f"Selected Bluetooth proxy {source} has no free connection slot")
            raise BleakError(f"Selected Bluetooth proxy {source} is unavailable or cannot see the lock")

    return RoutedClient
