"""Prepare, never send, paired DP3 probes for a dedicated HA test credential.

Input is a current list_credentials JSON response from an integration build
that records device_policy at enrollment. Existing/attributed records without
that evidence are deliberately rejected. Output contains no PIN digits.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import struct
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "credential_schedule", ROOT / "custom_components/tuya_ble_access/credential_schedule.py"
)
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)


def prepare(response, credential_id, device_id, now=None):
    now = time.time() if now is None else now
    matches = [c for c in response["credentials"] if c["credential_id"] == credential_id]
    if len(matches) != 1 or not matches[0]["name"].startswith("Pauzetest "):
        raise ValueError("Select exactly one dedicated Pauzetest credential")
    row = matches[0]
    policy = row.get("device_policy")
    if not policy or policy.get("hw_id") != row["hw_id"]:
        raise ValueError("Missing confirmed enrollment policy for this slot")
    if {1: "pin", 2: "card", 3: "fingerprint"}.get(policy.get("cred_type")) != row["type"]:
        raise ValueError("Credential type does not match enrollment")
    pause = schedule.build_schedule_probe(policy, paused=True)
    restore = schedule.build_schedule_probe(policy, paused=False)
    start, end = struct.unpack(">II", restore[5:13])
    if not start <= now < end:
        raise ValueError("Test access must be within its original validity")
    def action(payload):
        return {"action": "tuya_ble_access.probe_records", "data": {
            "device_id": device_id, "dp": 3, "payloads": [payload.hex()],
            "collect_seconds": 1}}
    return {"credential_id": credential_id, "name": row["name"],
            "scope": "one credential; firmware behavior unverified",
            "original_policy": policy, "restore_action": action(restore),
            "pause_action": action(pause),
            "expected_ack_hex": (restore[:5] + b"\x00\xff").hex(),
            "sequence": ["Verify baseline physical access", "Send pause and check matching ACK",
                         "Verify physical rejection", "Send restore and check matching ACK",
                         "Verify same credential opens again"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path)
    parser.add_argument("credential_id")
    parser.add_argument("device_id")
    args = parser.parse_args()
    print(json.dumps(prepare(json.loads(args.response.read_text()), args.credential_id,
                             args.device_id), indent=2))
