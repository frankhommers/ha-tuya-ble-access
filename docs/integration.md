# Integration guide

Tuya BLE Access is a Home Assistant custom integration for local Bluetooth lock
control. Installation and supported profiles are described in the [README](../README.md).
The tested behavior and remaining physical checks for version 0.2.1 are listed in the [release notes](releases/0.2.1.md).

## Setup

Create one **Tuya BLE Access** hub via Settings → Devices & services. Choose a
Tuya/Smart Life cloud account or local credentials. Cloud setup retrieves the
lock credentials; local setup uses credentials previously obtained for the
selected lock. Compatible unbound V5 devices also expose local activation.
Daily BLE commands are local, while explicitly selected cloud operations still
use the cloud account.

Keep the lock in Bluetooth range and close the Tuya lock panel during setup or
troubleshooting: the phone app and HA can compete for the available connection.
A proxy must support active GATT connections, even if it scans passively.

## Entities and actions

Available entities depend on the device profile:

- Lock control and battery/status sensors.
- Last unlock method, name, credential, time and recent history.
- Credential overview with current temporary PINs, expired cleanup candidates
  and an archive count.
- Profile-specific volume, auto-lock, passage/privacy and other settings.
- Local/cloud refresh actions and physical-reset reporting.

Names and actions are translated into English and Dutch. Automation-facing
select values use stable keys, independent of the display language.

Manage enrollment and temporary access through the actions in
Developer tools → Actions. See [credential management](credential-management.md).

## Connections and events

Leave persistent connection off for normal battery-conscious use. K3
advertisements expose an event flag; the integration connects to retrieve
records when that flag changes. It also performs a periodic alarm/status sweep
so events without an advertisement flag can be retrieved later.

The lock can buffer records while disconnected. Reports contain device IDs;
HA resolves known IDs to locally stored names. Old records contribute to history
and duplicate reports are suppressed. A motor transition can emit a generic
unlock event before the identifying record arrives.

Use `tuya_ble_access_temporary_code_used`, filtered by `password_id`, for a specific
temporary PIN. It includes an `attributed` flag so unknown IDs can be distinguished
from confirmed name mappings.

Persistent mode keeps reconnecting when a device drops its link and can increase
battery use. Normal idle disconnection is at least 20 seconds, extended as needed
for the configured auto-lock delay. Temporary-PIN cleanup uses existing normal
connections and does not introduce another periodic connection schedule.

## Reset and re-pairing

After physically factory-resetting a lock, use **Report hardware factory reset**
(`tuya_ble_access.report_factory_reset`) or its device button. This clears that
lock's HA credential mappings and temporary-code archive; it keeps people and
other locks. It does not reset the device or restore its Bluetooth pairing.

Re-pairing can rotate device credentials. Reconfigure or set up the device with
its current credentials before trying further BLE operations. Do not interpret
an old HA mapping as proof that a credential still exists in the reset lock.

## Troubleshooting

- If connecting fails, wake the keypad, check range and proxy availability, and
  close other apps currently connected to the lock.
- If an entity is absent, check whether the device profile supports it.
- If an unlock ID is unknown, enroll/map the credential through HA; matching a
  validity period alone is not evidence of which temporary code was used.
- A temporary-code action reports failure if the device does not return the
  expected success response. These protocol paths still need physical firmware
  validation as noted in the release notes.
- When filing an issue, include HA/integration versions, the product ID and the
  exact action/error. Review and redact logs or diagnostics before sharing
  device keys or account information.
