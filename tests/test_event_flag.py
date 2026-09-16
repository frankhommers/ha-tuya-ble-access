"""Tests for the advertisement event flag.

The K3 BLE PRO 2 flips bit 0x02 of the first FD50 service-data byte after a
successful unlock and clears it again ~30s later. Watching that bit lets the
integration connect only when the lock actually has something to report,
instead of holding a battery-draining persistent connection.

Observed on the real device (2026-08-29): resting 0x59, raised 0x5b, with the
remaining 11 service-data bytes unchanged.

Run with:
    PYTHONPATH=. pytest tests/test_event_flag.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble_access"


def _load(name: str):
    pkg = sys.modules.setdefault("_tblpkg", types.ModuleType("_tblpkg"))
    pkg.__path__ = [str(ROOT)]
    fq = f"_tblpkg.{name}"
    if fq in sys.modules:
        return sys.modules[fq]
    spec = importlib.util.spec_from_file_location(fq, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fq] = module
    spec.loader.exec_module(module)
    return module


event_flag = _load("event_flag")

# Real payloads captured from DC:23:51:D1:85:77.
RESTING = bytes.fromhex("590c0008bf076b6e7a44d483")
RAISED = bytes.fromhex("5b0c0008bf076b6e7a44d483")
MASK = 0x02


class TestParseEventFlag:
    def test_resting_payload_reads_low(self):
        assert event_flag.parse_event_flag(RESTING, 0, MASK) is False

    def test_raised_payload_reads_high(self):
        assert event_flag.parse_event_flag(RAISED, 0, MASK) is True

    def test_only_the_masked_bit_matters(self):
        # Every other bit differs, the masked one does not.
        assert event_flag.parse_event_flag(b"\xff", 0, MASK) is True
        assert event_flag.parse_event_flag(b"\xfd", 0, MASK) is False

    def test_short_payload_is_unknown(self):
        assert event_flag.parse_event_flag(b"", 0, MASK) is None
        assert event_flag.parse_event_flag(RESTING, 99, MASK) is None


class TestEventFlagTracker:
    def _tracker(self, cooldown=120.0):
        return event_flag.EventFlagTracker(cooldown_seconds=cooldown)

    def test_rising_edge_triggers(self):
        tracker = self._tracker()
        assert tracker.update(False, now=0.0) is False
        assert tracker.update(True, now=10.0) is True

    def test_first_observation_high_triggers(self):
        """A record may already be pending when HA starts up."""
        assert self._tracker().update(True, now=0.0) is True

    def test_staying_high_does_not_retrigger(self):
        tracker = self._tracker()
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(True, now=7.0) is False
        assert tracker.update(True, now=14.0) is False

    def test_second_unlock_triggers_again_after_cooldown(self):
        tracker = self._tracker(cooldown=120.0)
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(False, now=30.0) is False
        assert tracker.update(True, now=200.0) is True

    def test_cooldown_suppresses_rapid_retrigger(self):
        tracker = self._tracker(cooldown=120.0)
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(False, now=30.0) is False
        assert tracker.update(True, now=60.0) is False

    def test_second_unlock_while_flag_still_high_retriggers(self):
        """The flag stays high 21-30s, so a second unlock inside that window
        produces no rising edge. Without a re-trigger that event is lost."""
        tracker = self._tracker(cooldown=15.0)
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(True, now=10.0) is False   # same episode
        assert tracker.update(True, now=35.0) is True    # long enough: look again

    def test_short_cooldown_allows_a_quick_second_unlock(self):
        tracker = self._tracker(cooldown=15.0)
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(False, now=20.0) is False
        assert tracker.update(True, now=25.0) is True

    def test_unknown_reading_does_not_clear_state(self):
        """A malformed advertisement must not look like a falling edge."""
        tracker = self._tracker()
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(None, now=5.0) is False
        assert tracker.update(True, now=10.0) is False

    def test_dropout_during_episode_does_not_retrigger(self):
        """35% of advertisements are lost; gaps must not fake a new event."""
        tracker = self._tracker(cooldown=120.0)
        assert tracker.update(True, now=0.0) is True
        assert tracker.update(None, now=7.0) is False
        assert tracker.update(True, now=21.0) is False
