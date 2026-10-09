"""Merge a fresh controller poll into last-known-good socket states.

Why this module exists: after Home Assistant sends a switch command, the
controller force-reads the strip *before the strip's echo lands* and caches
that not-yet-true reading for its whole poll window (~2 s). The strip's own
mid-transition status blocks can also carry transient values on channels
whose relays never moved. Rendered verbatim, either one shows up in Home
Assistant as the *other* switches flipping and flipping back - relays
untouched, user alarmed, nothing actually wrong.

So a poll is not trusted blindly while a command is still settling. The
window opens the moment the tap starts - before the POST even returns -
because a poll landing in that half-second would otherwise revert the tap
display to pre-tap truth:

* the commanded socket renders the tapped value at once, so the tap display
  never reverts while the command is in flight or the echo travelling. If
  the command fails, the window is released at once and strip truth wins
  again; if the echo never confirms, it wins when the window expires;
* every other socket holds its last-good value for the whole window, with
  no early release: polls landing inside one controller cache generation
  are not independent evidence - two agreeing polls in a row are usually
  the same cached transient read twice, and trusting the second one is how
  a ghost once got accepted as truth. Strip truth wins when the window
  expires;
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

# How long after a tap the other sockets hold last-good (seconds).
# Covers the controller's ~2 s cache pin plus the strip's 1-2 s echo, with
# room for a full poll cycle on top. There is deliberately no early release:
# see merge_switches.
QUARANTINE_SECONDS = 30.0
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
    invalid_streak: int = 0
    last_body: dict[str, Any] | None = None


@dataclass
class MergeResult:
    """What one poll produced."""

    merged: dict[int, bool]
    held: tuple[int, ...] = ()
    frozen: bool = False
    dropped: bool = False
    # Commanded sockets whose window expired with the strip still reporting
    # something other than the tapped value: the tap display reverts here.
    # Empty when the echo confirmed in time or there was no window.
    expired: tuple[int, ...] = ()


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
    """Record a tap: this socket is settling, the rest wait.

    Called when the tap starts, not when the POST returns - a poll landing
    in between must already see the window open. The latest command always
    wins: a second tap restarts the window and discards unconfirmed evidence
    from the previous one, because mixed evidence from two overlapping
    transitions is exactly what must not be trusted.
    """
    memory.commanded = socket
    memory.want = want
    memory.deadline = now + QUARANTINE_SECONDS
    return memory


def command_failed(memory: StripMemory) -> StripMemory:
    """Release the window: the tap did not land, show strip truth.

    Called when the POST is refused or fails. Whatever the tap displayed
    while in flight is withdrawn at the next poll - which is the honest
    outcome, because a failed tap changed nothing. Last-good and the
    malformed-body streak are kept: they describe readings, not commands.
    """
    memory.commanded = None
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
        expired: tuple[int, ...] = ()
        if reported.get(memory.commanded) != memory.want:
            # The echo never confirmed: the tap display reverts to strip
            # truth on this poll, after showing the tapped value all window.
            expired = (memory.commanded,)
        memory.commanded = None
    else:
        expired = ()
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
            continue
        if in_window and socket == memory.commanded:
            # The user just tapped this socket: show the tapped value at
            # once. A pre-echo poll would otherwise revert the tap display
            # to the old state for a cycle - the exact bounce this module
            # exists to kill. `good` still tracks what the strip actually
            # reports, so if the echo never confirms, the deadline falls
            # back to strip truth.
            merged[socket] = memory.want
            memory.good[socket] = value
            continue
        if in_window:
            # During any quarantine window, hold ALL sockets at last-good
            # to prevent cross-socket contamination from the controller's
            # stale poll cache (2s window). This prevents "other sockets
            # bounce" when one socket is toggled.
            merged[socket] = known
            held.append(socket)
            continue
        if value == known:
            merged[socket] = value
            continue
        merged[socket] = value
        memory.good[socket] = value
    return MergeResult(merged=merged, held=tuple(held), expired=expired)
