# Temporary credential suspension

Research date: 2026-09-27; physical follow-up through 2026-09-30.
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

## Follow-up: ordinary PINs, cards and fingerprints (2026-09-28)

Rechecked panel modules 1051 and 1063 against the current DP reference.
DP3 supports member validity edits and individual method edits. However,
module 1063 actually sends a device edit only for password changes, including
the new PIN; card/fingerprint name edits are cloud metadata operations.
Therefore that panel alone does **not** prove a zero-PIN-length DP3 update
preserves an ordinary PIN, or that the no-weekdays pattern works for DP3.

The new research helpers build separate member and individual probes. DP3's
success byte is `ff`, unlike DP53's `00`; validation matches all identity bytes.
A member probe requires an independently confirmed member schedule: copying
one credential's schedule to its entire member could change other access.

New HA enrollments now record the exact validity, device member, role and
unlimited-use policy after a matching completion response. PIN digits and
biometrics are excluded. Legacy and locally re-attributed entries remain
unknown; HA's person attribution does not establish the device member ID.
The completed fingerprint test below enables the fingerprint-only capability
in version 0.3.5. Other ordinary credential types remain disabled.

`python scripts/prepare_credential_pause_probe.py RESPONSE.json CREDENTIAL_ID DEVICE_ID`
prepares an offline plan from a current `list_credentials` response. It requires
a dedicated non-admin test credential named `Pauzetest ...`, confirmed enrollment
policy and unexpired access. It sends nothing and includes the exact restore
command before the pause command. Run it using the development build that saves
the policy; v0.3.4 does not contain this follow-up metadata yet.

Initially, production HA had no registered ordinary credential to test. A new
non-admin test fingerprint was then enrolled under a dedicated test member.
Read-only DP54 sync confirmed its slot and device member, and the user confirmed
it opened the lock. A targeted DP3 update replaced only the recurrence with
no enabled weekdays; the matching seven-byte response ended in `00ff` (success).
The user confirmed rejection. Restoring the exact original enrollment validity
returned the same matching success response, and the user confirmed that the
same fingerprint opened again, without re-enrollment.

This verifies the immediate fingerprint deny/restore cycle on this Keybox.
Keybox restart/day rollover and effects on another enrolled credential were not
physically tested. Cards, ordinary PINs and member-wide suspension remain
unverified. Version 0.3.5 exposes only non-admin HA-enrolled fingerprints whose
original policy was captured by the new code. The pre-update test fingerprint
was restored manually using the observed enrollment workflow and DP54 identity;
its legacy HA record is not silently upgraded to a known-policy record.

## Follow-up: card verification completed

A new non-admin test card was enrolled for a separate test member. HA registration
and a read-only DP54 query identified its exact card slot and device member.
The user confirmed baseline access. A targeted DP3 no-weekdays update returned
the matching seven-byte success response; the user confirmed that the card was
rejected while the previously restored test fingerprint still opened the lock.
The exact original validity was then restored, the device acknowledged success,
and the user confirmed that the same card opened again.

Version 0.3.6 enables cards alongside fingerprints for the verified ba2qk177
profile. This test establishes isolation from the other test member's fingerprint
in this direction; it does not establish behavior for every combination of
methods/members. Reboot and day-rollover persistence remain untested. Ordinary
PINs and member-wide suspension still require separate physical verification.
Both test credentials are restored. Further physical testing was deferred by
the user after moving the Keybox indoors; no reset or new pairing is needed.

## Follow-up: ordinary PIN pause ineffective (2026-09-30)

A dedicated non-admin ordinary test PIN was enrolled through HA, which captured
its exact device identity and original schedule. The user confirmed baseline
access. An individual DP3 no-weekdays update preserved both dates and sent zero
PIN length, as in the verified card/fingerprint strategy.

The matching seven-byte device response ended in `0000`, rather than the
`00ff` success response observed for cards and fingerprints. The user then
confirmed that the same PIN **still opened the Keybox**. This establishes that
this no-PIN-content schedule update did not pause this ordinary PIN; it does
not establish the cause or whether another supported update format can work.
Ordinary PIN pause remains disabled. Do not relax acknowledgement validation
or infer support from the successful temporary-PIN, card or fingerprint tests.

The exact original schedule was subsequently sent back, again with zero PIN
length. Its matching response also ended in `0000`, so restoration was not
confirmed by the known success acknowledgement. The last physical observation
is that the original PIN still opens; no deletion or re-enrollment was performed.
After this restore attempt, the user reported the Keybox announcing
"Operation Failed". No further hardware commands were sent.

Reinspection of panel module 1063 confirms its ordinary-PIN edit includes a
nonzero PIN length and the PIN digits; module 692 encodes each digit as one
byte (`0` through `9`), not ASCII. The next prepared candidate retains the
same credential identity, dates and PIN, changing only the recurrence. It
requires the user to supply the identical test PIN. Pause and restore payload
construction were checked offline, including leading-zero PINs and rejection
of empty input. The user then entered the same test PIN and submitted the
no-weekdays update. This returned the matching `00ff` success response; the
user heard success and confirmed that the same PIN no longer opened the lock.
The original schedule was subsequently submitted with the same PIN and returned
the matching `00ff` success response. The user confirmed the same PIN opened
again, completing the pause/rejection/restore/access cycle without re-enrollment.
PIN digits are not included in this research record. Ordinary-PIN pause requires
PIN content on this tested firmware; the current implementation work adds that
input to the HA actions. Member-wide suspension remains unverified.

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
