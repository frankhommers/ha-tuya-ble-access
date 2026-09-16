# Migration to Tuya BLE Access

The GitHub repository is `frankhommers/ha-tuya-ble-access`. The Home Assistant
integration domain, service namespace, event prefix and storage prefix are now
`tuya_ble_access`. The project name is Tuya BLE Access.

## Existing installations

1. Back up Home Assistant. Update the old integration first if it still uses
   per-lock configuration entries: migration supports one enabled version-2 hub.
2. Install `custom_components/tuya_ble_access/` alongside the old component and
   restart Home Assistant. With HACS, ensure the old component directory remains
   available until migration finishes; restore it from the backup if necessary.
3. Add Tuya BLE Access and confirm the migration screen. Bluetooth discovery also
   offers this migration when an old hub exists.
4. Verify the existing devices and entity IDs. The migration copies both stores,
   including people, credential assignments, temporary PINs and archived names.
   Entity customizations, device IDs and entity IDs are retained through HA APIs.
5. Update service calls and event triggers in automations. For example,
   `tuya_ble_lock.create_temp_password` becomes
   `tuya_ble_access.create_temp_password`, and
   `tuya_ble_lock_temporary_code_used` becomes
   `tuya_ble_access_temporary_code_used`. No old-domain service aliases are kept.
6. Remove the old component directory after success and restart. The original
   storage files remain as a snapshot; do not restore or re-enable the old
   integration alongside the new one.

The migration disables and unloads the old hub before moving registries, then
removes its config entry. It does not enroll credentials, reset the physical lock
or change the lock's keys. New hub setup performs its normal status connection.

If migration is interrupted, reload the new integration or restart Home Assistant
to resume. Different data already present in either new-domain store is never
overwritten automatically; restore the pre-migration HA backup if the conflict
cannot be resolved. Config-entry IDs change, so external tooling that stores
those IDs must use the new entry. Entity and device IDs remain unchanged.
