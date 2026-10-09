"""Merge a fresh controller poll into last-known-good socket states.

Why this module exists: after Home Assistant sends a switch command, the
controller force-reads the strip *before the strip's echo lands* and caches
that not-yet-true reading for its whole poll window (~2 s). The strip's own
mid-transition status blocks can also carry transient values on channels
whose relays never moved. Rendered verbatim, either one shows up in Home
Assistant as the *other* switches flipping and flipping back - relays
untouched, user alarmed, nothing actually wrong.

So a poll is not trusted blindly while a command is still settling:

* the commanded socket always renders what the strip reports - no optimistic
  flip, and convergence the moment the echo lands;
* every other socket holds its last-good value until the new value is
  confirmed by two consecutive valid polls, or the window expires;
* a body that is not even well-shaped (no `switches` list, or sockets other
  than exactly {1, 2, 3, 4}) carries no information at all: the previous
  body is reused for a couple of polls, and only if the rot persists does
  the strip read as unavailable.

Everything here is plain data in, plain data out - the clock arrives as
`now`, so tests pass their own - and nothing is imported from Home
Assistant. The whole policy is exercised by `tests/test_state_merge.py`
with no Core installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOCKETS: tuple[int, ...] = (1, 2, 3, 4)

# How long after a command the other sockets need confirmation (seconds).
# Covers the controller's ~2 s cache pin plus the strip's 1-2 s echo, with
# room for one full poll cycle on top.
QUARANTINE_SECONDS = 12.0
# Consecutive agreeing polls that promote a divergent reading to truth.
CONFIRM_POLLS = 2
# Consecutive malformed bodies before a strip reads as unavailable instead
# of frozen.
INVALID_ESCALATE_POLLS = 3


@dataclass
class StripMemory:
    """What the merger remembers between polls, per strip."""

    good: dict[int, bool] = field(default_factory=dict)
    commanded: int | None = None
    want: bool = False
    deadline: float = 0.0
    # socket -> [value, consecutive count], for divergent readings inside
    # the window that have not confirmed yet.
    confirm: dict[int, list] = field(default_factory=dict)
    invalid_streak: int = 0
    last_body: dict[str, Any] | None = None


@dataclass
class MergeResult:
    """What one poll produced."""

    merged: dict[int, bool]
    held: tuple[int, ...] = ()
    frozen: bool = False
    dropped: bool = False


def parse_switches(body: Any) -> dict[int, bool] | None:
    """Extract socket -> on from a state body, or None when malformed.

    Malformed means the body carries no trustworthy per-socket reading at
    all: not a dict, no `switches` list, a non-integer socket, a missing
    `on`, a duplicate socket, or anything but exactly sockets 1-4. The
    strictness is the point - a truncated mid-transition block must read
    as "no information", never as "everything is off".
    """
    if not isinstance(body, dict):
        return None
    switches = body.get("switches")
    if not isinstance(switches, list):
        return None
    out: dict[int, bool] = {}
    for entry in switches:
        if not isinstance(entry, dict):
            return None
        socket = entry.get("socket")
        if not isinstance(socket, int) or socket not in SOCKETS:
            return None
        if "on" not in entry:
            return None
        if socket in out:
            return None
        out[socket] = bool(entry.get("on"))
    if set(out) != set(SOCKETS):
        return None
    return out


def note_command(
    memory: StripMemory, socket: int, want: bool, now: float
) -> StripMemory:
    """Record a successful command: this socket is settling, the rest wait.

    The latest command always wins - a second tap restarts the window and
    discards unconfirmed evidence from the previous one, because mixed
    evidence from two overlapping transitions is exactly what must not be
    trusted.
    """
    memory.commanded = socket
    memory.want = want
    memory.deadline = now + QUARANTINE_SECONDS
    memory.confirm = {}
    return memory


def merge_switches(
    memory: StripMemory, reported: dict[int, bool] | None, now: float
) -> MergeResult:
    """Fold one poll's socket readings into memory.

    `reported` is the output of `parse_switches`, or None when the body
    was malformed. Returns what the entities should render.
    """
    if reported is None:
        memory.invalid_streak += 1
        if (
            memory.invalid_streak >= INVALID_ESCALATE_POLLS
            or memory.last_body is None
        ):
            # Persistently unreadable, or never readable at all: no reading.
            # The caller turns the strip unavailable.
            return MergeResult(merged={}, dropped=True)
        # A lone bad block: freeze the last good frame, stay available.
        return MergeResult(merged=dict(memory.good), frozen=True)
    memory.invalid_streak = 0

    if memory.commanded is not None and now >= memory.deadline:
        memory.commanded = None
        memory.confirm = {}
    in_window = memory.commanded is not None and now < memory.deadline

    merged: dict[int, bool] = {}
    held: list[int] = []
    for socket in SOCKETS:
        value = reported[socket]
        known = memory.good.get(socket)
        if known is None:
            # Never seen a reading for this socket: a read, even an early
            # one, beats inventing one.
            merged[socket] = value
            memory.good[socket] = value
            memory.confirm.pop(socket, None)
            continue
        if in_window and socket == memory.commanded:
            # The commanded socket renders strip truth, always: pre-echo it
            # shows the old state (honest), post-echo the new one.
            merged[socket] = value
            memory.good[socket] = value
            memory.confirm.pop(socket, None)
            continue
        if value == known:
            merged[socket] = value
            memory.confirm.pop(socket, None)
            continue
        if in_window:
            pending = memory.confirm.get(socket)
            if pending is not None and pending[0] == value:
                count = pending[1] + 1
                if count >= CONFIRM_POLLS:
                    merged[socket] = value
                    memory.good[socket] = value
                    memory.confirm.pop(socket, None)
                else:
                    memory.confirm[socket] = [value, count]
                    merged[socket] = known
                    held.append(socket)
            else:
                memory.confirm[socket] = [value, 1]
                merged[socket] = known
                held.append(socket)
            continue
        merged[socket] = value
        memory.good[socket] = value
        memory.confirm.pop(socket, None)
    return MergeResult(merged=merged, held=tuple(held))
