# Integration guide

Tuya BLE Access is a Home Assistant custom integration for local Bluetooth lock
control. Installation and supported profiles are described in the [README](../README.md).
The changed activation behavior and remaining physical checks for version 0.3.0
are listed in the [release notes](releases/0.3.0.md).

## Setup

1. Pair a new lock in **Tuya Smart** or **Smart Life** first. Do not reset an
   already-paired lock to add it to Home Assistant.
2. Open **Tuya BLE Access** in Settings → Devices & services and start adding a
   device/hub. Sign in with the same account on first setup.
3. The import retrieves the account's devices, separate product information and
   available Bluetooth keys in one login. Records are saved locally by MAC.
4. Choose a recognized lock from **Add a saved lock**. A lock does not need to
   advertise, be awake or be in Bluetooth range to appear in this list.
5. For actual local commands, keep the lock near a Bluetooth adapter/proxy and
   close any phone app holding its connection. Radio reachability and whether
   the saved keys work can only be verified by communicating with the lock.

With an existing hub, its native **Add hub** action now opens **Add a lock**:
choose **Import devices and keys from Tuya** or **Choose a saved lock**. Importing
populates the registry; selecting a lock creates its active HA device. The list
shows names, MACs, product IDs, categories and key completeness, never key values.
Unknown types stay unknown and are not offered as locks. A generic Tuya/BLE name
is not type evidence. Product references are joined by exact product ID; no K3
product ID is ever used as a default, including in advanced manual setup.

Bluetooth discovery does not log in automatically. A previously saved bound lock
can be imported from its local keys; an unknown bound device offers an explicit
account import. Unbound discovery retains a separate recovery confirmation.

The advanced local setup option imports previously obtained credentials for an
already-bound lock. It does not pair a new or factory-reset lock.
Compatible unbound V5 devices also expose reactivation for previously paired
locks. A discovered `TyOS` device first shows **Bluetooth lock found: check
credentials**. Merely discovering it does not log in to Tuya or pair it. After
continuing, the flow looks up the MAC address in the local device records and saved
activation keys first. Only if neither has complete, valid credentials does it
consult the configured Tuya account. Devices absent from that account are directed
to app pairing; incomplete credentials never lead to Bluetooth activation.

The confirmation identifies locally saved keys or checked Tuya credentials.
Cloud credentials are saved before the Bluetooth attempt, separately from active
devices. A failed attempt does not add a lock, but its keys remain available for
a subsequent attempt without cloud access. Existing device records take precedence
over an older cached activation seed. Bluetooth still has to verify the keys.
This does not replace initial app pairing for a new lock. The advanced `activate`
action still fetches credentials from Tuya; use the discovery confirmation for
reactivation with locally saved keys.

The initial Bluetooth device-info phase has a 45-second deadline. This is before
any PAIR command is sent; later pairing/verification and persistence complete
normally. On connection errors, close the phone app, stop competing connections,
and keep the lock awake near the proxy. Do not reset a lock just because it timed
out. Daily use needs neither a reset nor repeated pairing.

Device identities and key generations are retained without automatic expiry in
Home Assistant's `.storage/tuya_ble_access_devices_key_history`, keyed by MAC.
New key sets append to history; consecutive identical snapshots are deduplicated.
Removing an active HA device does not erase this register. Existing device and
activation records are copied into it on access without overwriting a newer
record. A failed key fetch retains the available partial record, and failures on
one lock do not prevent importing the other account devices. Devices without a
valid MAC cannot be included in this MAC-indexed BLE registry.

Back up this file, `tuya_ble_access_devices_activation` and
`tuya_ble_access_devices` with the HA configuration: these stores contain private
device keys. Account passwords and raw DP payloads are excluded from the key
history. The latest key generation is used; old generations are retained for
recovery but are not automatically tried or mixed. A reset or app re-pair can make
saved keys invalid. Use an explicit cloud refresh to retrieve replacement keys. Saving activation credentials
does not start a lock coordinator or change the device's pairing.

Daily BLE commands work without the app or Tuya Cloud, while explicitly selected
cloud operations still use the cloud account.

Keep the lock in Bluetooth range and close the Tuya lock panel during setup or
troubleshooting: the phone app and HA can compete for the available connection.
A proxy must support active GATT connections, even if it scans passively.

## Which flow and when to reset

| Current state | Next step | Factory reset? |
| --- | --- | --- |
| Lock is already paired in Tuya / Smart Life | Retrieve its keys and import it into HA; close the app panel for BLE access | No |
| Lock was already reset and advertises as `TyOS` | Open discovery, check saved/account credentials, then separately confirm reactivation | Do not reset it again |
| New lock with no saved or account credentials | Pair in Tuya / Smart Life first, then add to HA | Follow the manufacturer's initial setup; HA does not require a reset |
| No discovery or Bluetooth timeout | Wake the lock near the proxy, close the app and stop competing connections | No |

A factory reset deliberately erases the existing pairing/configuration. It is
not a discovery, update, import or timeout-recovery step. Use it only when you
intend to erase that configuration and have a recovery/setup route.

The `TyOS` credential-check screen stays open with an explanation when keys
cannot be obtained. You can retry that check without losing discovery. A
successful check opens a separate reactivation confirmation; it never starts
pairing by itself.

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

- If the Bluetooth advertisement monitor sees the MAC address but no discovery
  card appears, check that Tuya BLE Access is at least 0.2.4. Earlier versions
  could miss advertisements containing FD50 service data without a service UUID
  list or scan-response name. A missing `TyOS` name does not mean the lock is absent.
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
