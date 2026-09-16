"""Drain the lock's repeated event pushes right after a flag-triggered connect.

The K3 BLE PRO 2 pushes an unlock record (cmd 0x8007) live at the moment of the
unlock and repeats it a few times (~every 6s). A connect triggered by the
advertisement flag lands a couple of seconds later, so the reliable way to
catch the record is to keep draining the notification buffer for a short window
right after connecting — long enough to cover the T+6 / T+12 repeats — instead
of only relying on the incidental collect window inside the status query.

Kept free of Home Assistant imports so it can be unit-tested standalone.
"""
from __future__ import annotations

import time

# cmd of a v4 DP *event* frame (unlock records ride this); mirrors const.py.
EVENT_CMD = 0x8007


async def async_drain_event_pushes(
    session,
    *,
    window: float,
    poll: float = 2.0,
    event_cmd: int = EVENT_CMD,
    clock=time.monotonic,
) -> str:
    """Poll ``session`` for unsolicited frames until an event push arrives or
    ``window`` seconds elapse, dispatching everything collected.

    Returns "caught" as soon as an ``event_cmd`` (0x8007) frame is seen — the
    lock keeps repeating it, and downstream dedup handles the repeats, so there
    is no reason to keep listening once the first one lands. Returns "timeout"
    when the whole window passed quietly, and "disconnected" when the link
    dropped mid-window (which is a transport fact, not evidence about the lock).
    """
    deadline = clock() + window
    while clock() < deadline:
        if not session.is_connected:
            # Distinguish "listened the whole window, nothing came" from
            # "the link dropped under us": the latter says nothing about the
            # lock and must not be read as a missed event.
            return "disconnected"
        frames = await session._collect(timeout=poll)
        if not frames:
            continue
        session._dispatch_dp_reports(frames)
        if any(f.get("cmd") == event_cmd for f in frames):
            return "caught"
    return "timeout"
