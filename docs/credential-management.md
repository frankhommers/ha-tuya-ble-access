# Credential Management

This guide explains how to enroll and manage PINs, fingerprints, NFC cards, and temporary passwords on your Tuya BLE lock through Home Assistant.

All credential operations are performed via **HA service calls** and work entirely over local Bluetooth — no cloud needed.

## Overview

Your lock can store multiple credential types:

| Type | How it works | Enrollment time |
|------|-------------|-----------------|
| **PIN code** | Numeric code entered on the keypad | Instant |
| **Fingerprint** | Finger placed on sensor multiple times | ~30 seconds (4-6 touches) |
| **NFC card** | Card tapped on lock sensor | ~5 seconds |
| **Temporary password** | Time-limited PIN with start/end dates | Instant |

Each credential is associated with a **member** (a person). You can link members to Home Assistant person entities, or use standalone names.

## Adding a PIN Code

PINs are the simplest credential type. The lock accepts 6-10 digit codes.

### Via Developer Tools

1. Go to **Developer Tools > Services**
2. Select `tuya_ble_access.add_pin`
3. Fill in:
   - **Lock device**: Select your lock
   - **PIN code**: e.g. `123456`
   - **Person** (optional): Select an HA person entity
4. Click **Call Service**

The lock will beep to confirm the PIN was enrolled.

### Via YAML Service Call

```yaml
service: tuya_ble_access.add_pin
data:
  device_id: <your_lock_device_id>
  pin_code: "123456"
  person: person.john
```

### In an Automation

```yaml
automation:
  - alias: "Add guest PIN when guest arrives"
    trigger:
      - platform: state
        entity_id: input_boolean.guest_mode
        to: "on"
    action:
      - service: tuya_ble_access.add_pin
        data:
          device_id: <your_lock_device_id>
          pin_code: "9876"
          person: person.guest
```

### Adding PINs to Multiple Locks

The `add_pin` service accepts multiple devices — useful for enrolling the same PIN across all your locks at once:

```yaml
service: tuya_ble_access.add_pin
data:
  device_id:
    - <lock_1_device_id>
    - <lock_2_device_id>
  pin_code: "123456"
  person: person.john
```

## Adding a Fingerprint

Fingerprint enrollment is an interactive process. The lock guides the user through placing their finger on the sensor multiple times (typically 4-6 touches).

### Steps

1. Go to **Developer Tools > Services**
2. Select `tuya_ble_access.add_fingerprint`
3. Fill in:
   - **Lock device**: Select your lock
   - **Person** (optional): Select an HA person entity
4. Click **Call Service**
5. **Go to the lock** — the fingerprint sensor will light up
6. Place your finger on the sensor, lift, and repeat when prompted
7. The lock beeps on each successful touch and plays a confirmation sound when complete

The enrollment has a **60-second timeout**. If you don't complete all touches in time, enrollment is cancelled and you'll need to try again.

### Via YAML

```yaml
service: tuya_ble_access.add_fingerprint
data:
  device_id: <your_lock_device_id>
  person: person.john
```

### Tips

- Use a **dry, clean finger** — moisture or dirt causes failed reads
- Place your finger **flat and centered** on the sensor
- Slightly vary the angle on each touch for better recognition
- The lock reports progress — check HA logs if you want to see touch count

## Adding an NFC Card

Card enrollment registers an NFC or RFID card (13.56 MHz) with the lock.

### Steps

1. Go to **Developer Tools > Services**
2. Select `tuya_ble_access.add_card`
3. Fill in:
   - **Lock device**: Select your lock
   - **Person** (optional): Select an HA person entity
4. Click **Call Service**
5. **Tap the card** on the lock's NFC sensor within 30 seconds

The lock beeps to confirm the card was enrolled.

### Via YAML

```yaml
service: tuya_ble_access.add_card
data:
  device_id: <your_lock_device_id>
  person: person.john
```

## Creating a Temporary Password

Temporary passwords are time-limited PINs that only work within a specified date/time range. Ideal for guests, cleaners, or contractors.

Enter times in Home Assistant's configured time zone, or include an explicit
offset (`+02:00`, `+01:00`) or `Z` for UTC. The integration converts both endpoints
to UTC Unix timestamps independently of the server's time zone. A local time
that does not exist during the spring clock change is rejected. A time that
occurs twice during the autumn change requires an explicit offset in YAML.
The end time must be later than the start time.

### Via Developer Tools

1. Go to **Developer Tools > Services**
2. Select `tuya_ble_access.create_temp_password`
3. Fill in:
   - **Lock device**: Select your lock
   - **Password name**: e.g. `Cleaner March`
   - **PIN digits**: At least 6 digits, e.g. `432198`
   - **Start time**: When the password becomes active
   - **End time**: When the password expires

### Via YAML

```yaml
service: tuya_ble_access.create_temp_password
data:
  device_id: <your_lock_device_id>
  name: "Weekend Guest"
  pin_code: "567834"
  effective_time: "2025-03-07T14:00:00"
  expiry_time: "2025-03-09T12:00:00"
```

### In an Automation

```yaml
automation:
  - alias: "Create cleaning day password every Monday"
    trigger:
      - platform: time
        at: "06:00:00"
    condition:
      - condition: time
        weekday:
          - mon
    action:
      - service: tuya_ble_access.create_temp_password
        data:
          device_id: <your_lock_device_id>
          name: "Cleaner {{ now().strftime('%b %d') }}"
          pin_code: "{{ range(100000, 1000000) | random }}"
          effective_time: "{{ now().replace(hour=8, minute=0, second=0).isoformat() }}"
          expiry_time: "{{ now().replace(hour=18, minute=0, second=0).isoformat() }}"
      - service: notify.mobile_app_phone
        data:
          message: "The temporary PIN action has completed. Share the chosen PIN separately."

### Recognising a specific temporary code

Creation now requires a successful DP51 response containing a hardware ID
(`0x00`–`0xFE`). A timeout, malformed response or rejection does not save an
attribution mapping. The PIN name stays in Home Assistant; the device receives
the validity period and PIN using the documented DP51 format.

The action optionally returns `password_id`, `name`, `hw_id`, `effective_ts` and
`expiry_ts`. Use `response_variable` in a script to capture that response. These
records are also exposed as `temporary_passwords` by `list_credentials` and the
credentials sensor. The PIN digits are not stored in these records or returned.

A live DP55 unlock report resolves its hardware ID to the saved name. The name
appears as the last credential, in the `by` field and in unlock history. This
identifies the code that was used, not the person physically using it.

For an automation on a particular code, listen for
`tuya_ble_access_temporary_code_used`. It includes `mac`, `timestamp`,
`lock_event_ts`, `method`, `hw_id`, `password_id`, `credential` and `attributed`.
The event fires when the code report arrives, including when the motor already
emitted the generic `tuya_ble_access_unlock` event with attribution pending.
Repeated reports are deduplicated, and historical backlog does not trigger this
live event. Unknown IDs produce `attributed: false` and null name/password ID.

```yaml
triggers:
  - trigger: event
    event_type: tuya_ble_access_temporary_code_used
    event_data:
      password_id: "<password_id returned when creating the code>"
      attributed: true
actions:
  - action: logbook.log
    data:
      name: "Guest access"
      message: "Temporary code {{ trigger.event.data.credential }} was used."
```

Old temporary-code records without a confirmed hardware ID remain unattributed.
Overlapping validity periods never serve as evidence of identity. If an ID is
reused, older mappings remain available for delayed history; ambiguous records
are left unattributed. The implementation follows the
[Tuya Bluetooth DP reference](https://developer.tuya.com/en/docs/iot/ble?id=K9ow3vcpn71ua).
Actual creation, validity enforcement and reported IDs still require testing on
the physical K3 BLE PRO 2 firmware.

### Automatic cleanup after expiry

Expired temporary PINs with a confirmed hardware ID are removed through DP52 on
the next normal BLE connection or operation. Startup status refresh also performs
cleanup. This creates no additional scheduled connections; if the lock is offline,
cleanup waits until it is reachable during normal use. Times are compared as UTC
Unix timestamps.

A matching reply must confirm deletion or that the slot is already absent before
the record is archived. A timeout, wrong ID, malformed reply or rejection leaves
it pending for a later attempt, with at least 60 seconds between cleanup batches.
Codes replaced by a new registration in the same hardware slot are not deleted
using the old mapping. Creation and cleanup are serialized per lock.

The credentials sensor and `list_credentials` action expose:

- `temporary_passwords`: current and future codes.
- `expired_temporary_passwords`: expired codes awaiting cleanup. Legacy entries
  without a confirmed hardware ID cannot be deleted automatically.
- `archived_temporary_password_count`: confirmed removals and superseded mappings.

The archive retains the name, hardware ID, validity and lifecycle timestamps,
not the PIN digits. Buffered records synced from the lock can therefore still
receive their historical name. Archived mappings do not attribute records after
the confirmed deletion. A hardware factory-reset report also clears this archive.
Actual expiry enforcement and DP52 acknowledgements still need a physical test on
the lock firmware.
```

## Deleting Credentials

You can delete credentials by person, by type, or by specific credential ID.

### Delete All Credentials for a Person on a Lock

```yaml
service: tuya_ble_access.delete_credential
data:
  device_id: <your_lock_device_id>
  person: person.guest
```

### Delete Only Fingerprints for a Person

```yaml
service: tuya_ble_access.delete_credential
data:
  device_id: <your_lock_device_id>
  person: person.guest
  cred_type: fingerprint
```

### Delete a Specific Credential by ID

First, list credentials to find the ID:

```yaml
service: tuya_ble_access.list_credentials
data:
  device_id: <your_lock_device_id>
```

Then delete by ID:

```yaml
service: tuya_ble_access.delete_credential
data:
  device_id: <your_lock_device_id>
  credential_id: "abc123-def456-..."
```

### In an Automation (Remove Guest Access)

```yaml
automation:
  - alias: "Remove guest PIN when guest leaves"
    trigger:
      - platform: state
        entity_id: input_boolean.guest_mode
        to: "off"
    action:
      - service: tuya_ble_access.delete_credential
        data:
          device_id: <your_lock_device_id>
          person: person.guest
          cred_type: pin
```

## Listing Credentials

To see all credentials enrolled on a lock:

1. Go to **Developer Tools > Services**
2. Select `tuya_ble_access.list_credentials`
3. Select your lock device
4. Enable **Return response**
5. Click **Call Service**

The response shows all credentials grouped by member:

```json
{
  "credentials": [
    {
      "credential_id": "abc123...",
      "member": "John",
      "type": "password",
      "name": "John PIN",
      "hw_id": 1
    },
    {
      "credential_id": "def456...",
      "member": "John",
      "type": "fingerprint",
      "name": "John Fingerprint",
      "hw_id": 2
    }
  ]
}
```

## After a Hardware Factory Reset

After physically resetting a lock, open its device page in Home Assistant and
press **Report hardware factory reset** (Dutch: **Slot is gereset**). You can also
call the administrator action in **Developer Tools > Actions**:

```yaml
action: tuya_ble_access.report_factory_reset
data:
  device_id: <your_lock_device_id>
```

This removes that lock's old credential mappings and temporary password records
from Home Assistant. People and records belonging to other locks are preserved.
The action works while the lock is offline and can safely be repeated. With a
response requested, it reports how many records were removed.

This reports a reset you have already performed; it does not reset the hardware
or restore Bluetooth pairing. After reconnecting the lock, enroll credentials
again, or use `register_credential` to associate credentials you have newly
enrolled through the lock itself. Old slot numbers are not automatically reused
as person mappings.

## Important Notes

- **Credentials are stored locally** in Home Assistant's `.storage/` directory. The lock itself has no way to list enrolled credentials over BLE — the integration tracks them locally during enrollment.
- **Factory reset clears all lock credentials** but HA's local records remain until you use **Report hardware factory reset** as described above.
- **Biometric templates never leave the lock** — fingerprint data is stored only in the lock's secure element. You cannot copy fingerprints between locks; each lock needs separate enrollment.
- **PINs are the only credential type that can be enrolled on multiple locks at once** via the multi-device `add_pin` service. Fingerprints and cards must be enrolled one lock at a time.
- **Admin vs regular credentials**: The `admin` flag may grant elevated privileges on the lock (e.g., ability to bypass the privacy lock). Most users should leave this as `false`.
- **Member IDs** are assigned automatically (1-100). The lock uses these internally to track who unlocked it.
