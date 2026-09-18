# Integration guide

Tuya BLE Access is a Home Assistant custom integration for local Bluetooth lock
control. Installation and supported profiles are described in the [README](../README.md).
The changed activation behavior and remaining physical checks for version 0.2.3
are listed in the [release notes](releases/0.2.3.md).

## Setup

1. Pair a new lock in the **Tuya Smart** or **Smart Life** app first.
2. Create one **Tuya BLE Access** hub via Settings → Devices & services.
   Choose **Add a lock via Tuya / Smart Life** and sign in with the same account
   to retrieve the lock's Bluetooth keys.
3. Close the app's lock panel and wake the lock near a Home Assistant Bluetooth
   adapter or proxy so Bluetooth discovery can find it.

To add another lock to that hub, pair it in the app with the same account, close
its app panel and wake it near Home Assistant. Bluetooth discovery adds it to the
existing hub. A hub using only local credentials must first be reconfigured with
a Tuya account to use this route.

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

Activation keys are stored locally by MAC in Home Assistant's `.storage`, in
`tuya_ble_access_devices_activation`, separately from the existing
`tuya_ble_access_devices` device records. Back up both stores with the rest of the
HA configuration; they contain private device keys. Saving activation credentials
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
