# Temporary credential suspension

Research date: 2026-09-27; physical verification completed 2026-09-28.
The initial source investigation sent no lock commands. The subsequent
authorized test changed only a dedicated temporary test PIN's validity.

## Physical result: temporary PIN verified

The user confirmed the complete cycle on the existing Home Assistant-connected
Keybox: the temporary PIN opened the lock, was rejected after its start time
was moved into the future, and opened the lock again after its original
validity was restored. No deletion, re-enrollment, factory reset or Tuya pairing
was involved. The restore response was `DP53=0100` (slot 1, success).
The initial modification response was not captured because the browser left
the action page; physical rejection was confirmed by the user.

A second complete physical cycle verified the production strategy: DP53 sets
weekly recurrence with **no enabled weekdays** (`02000000000000173b`), retaining
both original dates. The Keybox acknowledged the pause and restore with
`DP53=0100`; the user confirmed rejection while paused and access after restore.
Unlike the first experiment, this schedule has no future automatic resume time.

Version 0.3.4 implements admin-only pause/resume actions for this product profile.
It preserves the stored original validity, serializes credential operations,
and persists an unknown outcome before sending a command. Only a matching
successful acknowledgement marks the state confirmed. Original expiry and
expired-PIN cleanup still apply. No PIN digits are sent or stored by this flow.
Persistence across a Keybox reboot and day rollover have not been physically
tested; ordinary PINs, cards and fingerprints are not supported by these actions.

The source-only investigation below is historical evidence leading to these
tests, not a statement that the implemented temporary-PIN flow remains unverified.

## Result from the actual product panel

Retrieved the official `ba2qk177` panel **000000mtoi, version 1.5.26**, read-only
using the existing Tuya account. Its archive matches Tuya's hexadecimal MD5;
SHA-256: `4bb121b191b08330a26e585b3a6383b28be3da054f3f8d217e3297f9d3869242`.
The proprietary code remains outside this repository under the private
`research/credential-pause` directory.

| Access type | Implementation in this panel | Conclusion |
| --- | --- | --- |
| Shared mobile access, type 7 / subtype 0 | Cloud Freeze/Unfreeze | Real suspension flow for this specific access type |
| Temporary PIN, type 1 | BLE DP53 date/schedule edit, without resending PIN | No Freeze menu; schedule-based suspension subsequently verified |
| Offline generated password, type 3 | Delete/rename depending on subtype | No Freeze option in this menu |
| Member access | BLE DP3 member schedule edit | Candidate affecting a member; reversible suspension still unverified |
| Ordinary PIN/card/fingerprint | DP3 method editing, DP2 deletion | No general per-method freeze implementation found |

### Bundle evidence

Module IDs below refer to the downloaded `panel/main.jsbundle`.

- **941, `getMenuList`:** Freeze is offered only for type 7 / subtype 0;
  returned phase 3 selects Unfreeze. Type 7 is rendered as mobile access,
  routes to `editMobile`, and carries recipient `shareInfo`.
- **1073 -> 954 -> 670:** Freeze requests phase 4, unfreeze phase 2, passing
  `unlockBindingId` to `tuya.m.device.lock.key.virtual.phase` with `devId` added
  by the wrapper. The app then reloads the list. Requested phases 4/2 differ
  from displayed frozen phase 3. No mutation API was called in this research.
- **1076:** temporary PIN schedule editing sends DP53 with
  `hardware_id:u8 | 01 | validity:17 bytes | 00 | 00`.
  The last bytes are usage count and PIN length: **no PIN digits are sent**.
  After BLE result byte zero, the app updates cloud metadata. This corrects the
  earlier assumption that editing necessarily requires the original PIN.
- **1051:** member schedule editing sends DP3 with
  `00 | 00 | 00 | member_id:u8 | ff | validity:17 bytes | 00 | 00 | 00`.
  Preserve the observed three-byte tail when investigating this path. The app
  checks response type 00 and success ff before saving cloud member times.
- **1063:** method editing constructs DP3 from method type, role, member,
  hardware ID, validity, usage count and PIN length/content. That edit flow
  does not establish an explicit reversible pause state.
- **696:** freeze/unfreeze DP names occur in a generic name map, with no use
  of those mapped properties found. Names alone do not establish support.

### Retrieval and capture audit

Android call chain: `PreDownloadServiceImpl` -> `bppdbpp` -> `dpppdpp` ->
`qpbpqpq.pppbppp` calls `thing.m.product.ui.info.batch.get` v1.1 with `pids`
containing JSON-encoded `{pid, ver}` objects. `PanelLoadManager` calls
`s.m.ui.upgrade` v3.0 with the UI ID before the underscore, type and phase.
Supplying `appRnVersion=5.99` (from `RNAPIUtil/BuildConfig`) yielded the real
version and download URL; without it, metadata was incomplete. No phone was
needed to download the panel.

Both saved MITM flow files are empty. The capture inside `bugreport2.zip`
yielded **26 CRC-verified decrypted frames**, including three DP writes:
DP71 twice and DP46 once. None of those verified frames establishes suspension.
The other three captures yielded 28/45/62 reassembled messages respectively,
but no CRC-verified decryptions with the three tested credential sets; their
contents remain unknown. `tuya_pairing.pcap` contains IP/TLS traffic, not raw
Bluetooth packets.

The DP53 physical verification described above completes the temporary-PIN
investigation. DP3 member schedules remain unverified. The mobile-sharing
cloud API is not a universal credential pause operation.

## Initial finding (before panel retrieval and physical tests)

Tuya documents suspension, but the schema alone did not establish reversible
suspension for the ba2qk177 Keybox.

The [Bluetooth lock implementation guide](https://developer.tuya.com/en/docs/iot/ble-doorlock-developer-function-document?id=K9fwaai7m9wt3)
lists deprecated per-method freeze/unfreeze commands (DP4/5), and member-level
unfreeze/freeze commands (DP49/50). Member suspension affects that member's
associated unlocking methods.

The checked-in `ba2qk177.schema.json` contains none of DP4, DP5, DP49 or DP50.
This is missing advertised support, not proof that firmware rejects them.
Sending guessed commands to production is not an appropriate capability probe.

## Initial candidate

The schema does contain DP3 (`unlock_method_modify`) and DP53
(`temporary_password_modify`). The profile already mapped these before the pause/resume implementation.

Tuya's [DP reference](https://developer.tuya.com/en/docs/iot/ble?id=K9ow3vcpn71ua)
describes validity and usage-count changes through these commands. DP3 can
address members or individual methods. The usage-count field includes an
expired value; this does **not** establish that setting it can later be undone.
DP53's documented request includes PIN content. The product panel now provides
concrete evidence of a zero-length-PIN schedule update, as described above.

Hypothesis: changing validity could suspend access while retaining enrollment,
then restoring validity could resume it. Retention, reversibility, exact scope
and firmware behavior all require verification. An accepted Bluetooth response
alone is insufficient evidence that physical access was blocked.

## Constraints considered during implementation

- `CredentialRecord` stores identity and attribution, but no original schedule,
  usage limit or suspension status. It cannot currently restore these settings.
- `TempPasswordRecord` stores dates and hardware ID, but no PIN digits. A flow
  requiring the original PIN would need explicit input or a separately reviewed
  storage design.
- `temp_password_cleanup.py` deletes expired temporary PINs using DP52. Reusing
  expiry as suspension without coordinating cleanup could destroy the entry.
- Deleting and recreating an entry is not a general suspension mechanism:
  fingerprint enrollment cannot be reconstructed from the stored metadata.
- A Home Assistant flag alone cannot prevent the lock accepting local input.

## Verification before implementation

### Local reverse-engineering evidence checked

The follow-up search included the decompiled Android sources, original APK
assets, extracted iOS files and executable strings, and archived research tools
and notes. Searches for `unlock_method_freeze`, `unlock_method_unfreeze`,
`unlock_method_modify`, and `opmode.phase` did not locate an app implementation
of suspension. This is a search limitation, not evidence of unsupported firmware:
obfuscation, binary code or separately downloaded panel code can hide it.

The archived `research/ha-experiments/tools/lock_control.py` names DP3/53 and
implements permanent-validity encoding for enrollment, but does not implement
pause/resume. `tools/decode_btsnoop.py` likewise labels those DPs; labels alone
are not captured evidence of reversible suspension. The subsequent capture
audit and decryption limits are recorded above.

Following that initial search, the decompiled panel download infrastructure
provided the route to the actual device panel. Its findings are recorded above.

### Physical verification, once the command is established

1. Establish reliable BLE communication, and use a dedicated disposable test
   member/PIN. Obtain authorization for that test; leave existing access alone.
2. If the Tuya app exposes suspension for this exact device, observe its
   command and confirmation first. Distinguish member-wide and per-method scope.
3. Record the test entry's identity and original policy. Verify ordinary access,
   suspend it, physically verify rejection, restore it, and verify access again
   without enrollment. Check another test method to establish scope.
4. Repeat after reconnect and device restart. Confirm the entry remains present
   and that automatic cleanup cannot remove it. Test each claimed method type
   separately; PIN success does not establish fingerprint/card support.

If verified, model requested versus confirmed status per lock and credential,
persist the original policy, serialize changes with other credential operations,
and show pending/unknown on disconnect or ambiguous responses. Only advertise
capabilities validated for the specific product/firmware. Naturally expired
access must not become valid merely because a paused entry is resumed.
