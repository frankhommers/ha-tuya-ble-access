<p align="center"><img src="logo.png" width="128" height="128" alt="Tuya BLE Access"></p>

# Tuya BLE Access

Home Assistant custom integration for local Bluetooth control of Tuya locks and
keyboxes, by [Frank Hommers](https://github.com/frankhommers).
This repository contains the integration, its tests and user documentation.

**Version 0.2.1**, validated in Home Assistant 2026.8.3. Temporary
PIN creation, validity enforcement and removal still need physical validation on
the K3 BLE PRO 2. See [release notes](docs/releases/0.2.1.md).

## Features

- Local lock/unlock, battery/status monitoring and supported lock settings.
- Enrollment and attribution of PINs, fingerprints and NFC cards.
- English and Dutch entity names, actions, options and errors.
- K3 event-driven connections and unlock history with duplicate suppression.
- Report a hardware factory reset to clear obsolete mappings for that lock.
- Experimental temporary PINs: UTC-aware validity, confirmed hardware IDs,
  named unlock events and automatic cleanup after expiry.

## Devices

| Device | Product ID | Protocol | Validation |
|---|---|---|---|
| K3 BLE PRO 2 | `ba2qk177` | V5 | Main development/test device; temporary PIN lifecycle still experimental |
| Smart Lock 3 | `qqmu5mit` | V4 | Existing profile; not physically retested for this release |
| H8 Pro | `wwbdbt3h` | V3 | Existing profile; not physically retested for this release |

Other devices require a compatible profile. Similar Tuya branding does not
establish protocol compatibility.

## Installation

Requirements: Home Assistant **2026.8.3 or later** and a supported Bluetooth
adapter or ESPHome Bluetooth proxy. Earlier HA versions are not validated by
this release.

### HACS custom repository

1. Open HACS and add `https://github.com/frankhommers/ha-tuya-ble-access` as an Integration custom repository.
2. Install Tuya BLE Access.
3. Restart Home Assistant.
4. Add **Tuya BLE Access** under Settings → Devices & services.

### Manual installation

Extract the release ZIP into the Home Assistant configuration directory, or copy
`custom_components/tuya_ble_access/` there from this repository. Restart HA and add
the integration. The directory must end up at
`config/custom_components/tuya_ble_access/manifest.json`.

## Setup and daily use

The setup flow offers a Tuya/Smart Life cloud account or local credentials. Cloud
setup retrieves the device's BLE credentials. Local setup imports credentials
already obtained for the lock. Daily BLE operations do not require Tuya Cloud;
explicit cloud refresh/setup operations still contact Tuya.

Compatible unbound V5 locks also have local activation entry points. Activation
changes pairing state; use the HA flow's instructions for the selected device.

The integration groups locks under one hub. See the
[integration guide](docs/integration.md) and
[credential management guide](docs/credential-management.md) for details.

For lock battery life, leave **Persistent connection** disabled unless needed.
The K3 event flag can trigger a short connection to retrieve a new unlock record.
Normal idle disconnect is at least 20 seconds and may be extended for the
configured auto-lock delay. A periodic alarm/status sweep also remains enabled.

`bluetooth_proxy.active: true` enables GATT connections; it does not require a
permanent connection. ESPHome's separate active/passive *scan* setting controls
scan requests. Passive scanning has not been validated for the complete unlock
flow in this release.

## Temporary PINs (experimental)

Use the **Create temporary PIN** action (`tuya_ble_access.create_temp_password`),
with a name, at least six PIN digits, and start/end times. Times without an offset
use HA's configured time zone. Explicit UTC offsets and `Z` are respected; clock
change ambiguities require an explicit offset.

Creation must return a successful device response and hardware ID. The optional
response contains the stable local `password_id`. A matching live unlock report
emits `tuya_ble_access_temporary_code_used` with the name and `password_id`, suitable
for automations targeting a specific code. This identifies the code used, not
the physical person using it.

Expired codes are removed on normal connections, without additional periodic
connections. Only confirmed deletion or an already-absent response archives the
mapping. Old names remain available for delayed historical records. Current,
expired-awaiting-cleanup and archived records are exposed separately.

See [temporary PIN examples and limitations](docs/credential-management.md#creating-a-temporary-password).

## Upgrading

### From Tuya BLE Lock

The repository is now `frankhommers/ha-tuya-ble-access` and the integration domain
is `tuya_ble_access`. Back up Home Assistant, install the new integration and
restart. Keep the old component files installed for this migration. Add
**Tuya BLE Access** under Settings → Devices & services and confirm **Migrate
the existing integration**. This requires one enabled version-2 Tuya BLE Lock hub.

Migration preserves entity IDs, device IDs, local device credentials, people,
enrolled credentials and temporary-PIN history. The old config entry is removed;
its storage files are retained. After successful migration, remove the old
`custom_components/tuya_ble_lock/` directory and restart Home Assistant.

Update explicit action names (`tuya_ble_lock.*` → `tuya_ble_access.*`) and event
triggers (`tuya_ble_lock_*` → `tuya_ble_access_*`) in your automations. Entity-based
actions keep their existing entity IDs. See [migration details](docs/domain-migration.md).

Back up Home Assistant before upgrading. Older per-lock entries
are migrated to the hub model. A physical factory reset must be reported using
the **Lock was reset** action/button so stale hardware mappings are cleared;
people and other locks are retained.

Select values now use language-independent keys. Update automations that pass
English labels to `select.select_option`: for example, use `normal` instead of
`Normal`. Read each entity's `options` attribute for valid keys.

## Development

```sh
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python scripts/build_release.py
```

Tests do not communicate with a real lock. The build produces a verified ZIP
containing only the integration. See [release preparation](docs/releasing.md).
Captures, Android NFC Lab, app analyses and standalone research tools are kept
outside this Git repository and are excluded from release artifacts.

## Attribution

Tuya BLE Access is written and maintained by
[frankhommers](https://github.com/frankhommers). Publicly documented Tuya BLE
protocol work, including [redphx/python-tuya-ble](https://github.com/redphx/python-tuya-ble),
informed the framing and crypto layers; the K3 behaviour comes from this
project's own protocol research.

## License

This project is available under the [MIT License](LICENSE).
