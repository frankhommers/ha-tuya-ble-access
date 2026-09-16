"""Tests for the post-connect event-push drain (fast flag-triggered capture).

Proven on the real device 2026-08-31: a fast flag-triggered connect catches the
repeated 0x8007 unlock push. This drain widens the catch window so it works even
when the connect lands a little slower.
"""
from __future__ import annotations

import asyncio
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


event_collect = _load("event_collect")


class _FakeSession:
    """Serves queued frame batches from _collect and records dispatches."""

    def __init__(self, batches, *, connected=True):
        self._batches = list(batches)
        self.is_connected = connected
        self.dispatched: list[dict] = []

    async def _collect(self, timeout=2.0):
        return self._batches.pop(0) if self._batches else []

    def _dispatch_dp_reports(self, frames):
        self.dispatched.extend(frames)


class _Clock:
    """Deterministic monotonic clock that advances one tick per read."""

    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def test_returns_true_and_stops_on_first_event_push():
    async def run():
        session = _FakeSession([
            [{"cmd": 0x8006, "data": b""}],           # status noise, no event
            [{"cmd": 0x8007, "data": b"unlock"}],     # the unlock push
            [{"cmd": 0x8007, "data": b"unlock"}],     # repeat (must NOT be reached)
        ])
        got = await event_collect.async_drain_event_pushes(
            session, window=100.0, clock=_Clock(),
        )
        assert got == "caught"
        # Stopped right after the first event; the repeat batch is untouched.
        assert len(session._batches) == 1
        # Both the noise and the event frame were dispatched.
        assert [f["cmd"] for f in session.dispatched] == [0x8006, 0x8007]

    asyncio.run(run())


def test_returns_false_when_no_event_within_window():
    async def run():
        session = _FakeSession([[{"cmd": 0x8006, "data": b""}]] * 50)
        # window of 3 with a 1.0/tick clock => ~3 polls, never an 0x8007
        got = await event_collect.async_drain_event_pushes(
            session, window=3.0, clock=_Clock(step=1.0),
        )
        assert got == "timeout"

    asyncio.run(run())


def test_stops_when_session_disconnects():
    async def run():
        session = _FakeSession([[]], connected=False)
        got = await event_collect.async_drain_event_pushes(
            session, window=100.0, clock=_Clock(),
        )
        assert got == "disconnected"
        assert session.dispatched == []

    asyncio.run(run())
