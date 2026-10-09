#!/usr/bin/env python3
"""Test state_merge without a Home Assistant install.

The switch-flap this pins down: after Home Assistant sends a command, the
controller force-reads the strip before its echo lands and caches that
not-yet-true reading, and mid-transition status blocks can carry transient
values on untouched channels. Rendered verbatim, the *other* switches flip
and flip back while no relay moves.

The failure modes reading the code does not catch:

* a merge that trusts every poll - the flap itself;
* a hold that never releases - a switch stuck displaying yesterday;
* a malformed body read as "everything off" - the most alarming possible
  lie, and the shape a truncated block naturally takes;
* a quarantine that a second tap does not restart - mixed evidence from two
  overlapping transitions trusted as one.

Only the stdlib and this one module are needed. `state_merge.py` imports
nothing from Home Assistant, and the clock arrives as an argument, so the
whole policy - windows, confirmations, escalation - is exercised directly,
by path, without importing the package.
"""

from __future__ import annotations

import importlib.util  # noqa: E402
import sys
from pathlib import Path  # noqa: E402

_MODULE_DIR = Path(__file__).resolve().parent.parent / "custom_components/tonly_mttl_w01"

_spec = importlib.util.spec_from_file_location(
    "_tonly_state_merge", _MODULE_DIR / "state_merge.py"
)
assert _spec and _spec.loader
smx = importlib.util.module_from_spec(_spec)
# Dataclasses resolve their module through sys.modules at class-creation
# time; a by-path load has no entry yet, so register it first.
sys.modules[_spec.name] = smx
_spec.loader.exec_module(smx)

passed = 0
failed: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed.append(f"{label}: {detail}")
        print(f"  FAIL {label}  -- {detail}")


def body(on: dict[int, bool]) -> dict:
    return {"switches": [{"socket": s, "on": on[s]} for s in (1, 2, 3, 4)]}


def main() -> int:
    print("state_merge.merge_switches / note_command / parse_switches")
    now = 1000.0

    # -- shape validation ------------------------------------------------
    check(
        smx.parse_switches(body({1: True, 2: False, 3: True, 4: False}))
        == {1: True, 2: False, 3: True, 4: False},
        "a well-shaped body parses socket by socket",
    )
    check(
        smx.parse_switches({"switches": []}) is None,
        "an empty switches list is no information, not all-off",
    )
    check(
        smx.parse_switches({"sockets": [{"socket": 1, "on": True}]}) is None,
        "a body without a switches list is malformed",
    )
    check(
        smx.parse_switches(
            {"switches": [{"socket": s, "on": True} for s in (1, 2, 3)]}
        )
        is None,
        "a truncated block missing a socket is malformed",
    )
    check(
        smx.parse_switches(
            {"switches": [{"socket": s, "on": True} for s in (1, 2, 3, "4")]}
        )
        is None,
        "a non-integer socket is malformed",
    )
    check(
        smx.parse_switches(
            {"switches": [{"socket": 1}, {"socket": 2, "on": True},
                           {"socket": 3, "on": True}, {"socket": 4, "on": True}]}
        )
        is None,
        "an entry missing on is malformed, not off",
    )
    check(
        smx.parse_switches(None) is None,
        "no body at all is malformed",
    )

    # -- no window: every valid poll is truth -----------------------------
    mem = smx.StripMemory()
    res = smx.merge_switches(mem, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    check(
        res.merged == {1: False, 2: False, 3: False, 4: True} and not res.held and not res.dropped,
        "outside any window the first poll populates last-good",
    )

    # -- the commanded socket always renders strip truth -------------------
    smx.note_command(mem, 1, True, now)
    res = smx.merge_switches(mem, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now + 1)
    check(
        res.merged[1] is False and not res.held,
        "pre-echo the commanded socket still shows the old state",
    )
    res = smx.merge_switches(mem, smx.parse_switches(body({1: True, 2: False, 3: False, 4: True})), now + 3)
    check(
        res.merged[1] is True,
        "post-echo the commanded socket converges with no hold",
    )

    # -- non-commanded sockets hold, then confirm ---------------------------
    mem2 = smx.StripMemory()
    smx.merge_switches(mem2, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    smx.note_command(mem2, 1, True, now)
    res = smx.merge_switches(mem2, smx.parse_switches(body({1: True, 2: True, 3: False, 4: True})), now + 1)
    check(
        res.merged == {1: True, 2: False, 3: False, 4: True} and res.held == (2,),
        "a divergent non-commanded socket holds at last-good in the window",
    )
    res = smx.merge_switches(mem2, smx.parse_switches(body({1: True, 2: True, 3: False, 4: True})), now + 6)
    check(
        res.merged[2] is True and not res.held,
        "a change confirmed by two consecutive polls is accepted in-window",
    )

    # -- a lone transient never shows ----------------------------------------
    mem3 = smx.StripMemory()
    smx.merge_switches(mem3, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    smx.note_command(mem3, 1, True, now)
    res = smx.merge_switches(mem3, smx.parse_switches(body({1: True, 2: True, 3: False, 4: False})), now + 1)
    check(
        res.merged == {1: True, 2: False, 3: False, 4: True} and set(res.held) == {2, 4},
        "transient wrong values on untouched sockets never render",
    )
    res = smx.merge_switches(mem3, smx.parse_switches(body({1: True, 2: False, 3: False, 4: True})), now + 6)
    check(
        res.merged == {1: True, 2: False, 3: False, 4: True} and not res.held,
        "the next agreeing poll restores the real picture",
    )
    check(
        mem3.good == {1: True, 2: False, 3: False, 4: True},
        "held values never pollute last-good",
    )

    # -- the window ends -------------------------------------------------------
    mem4 = smx.StripMemory()
    smx.merge_switches(mem4, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    smx.note_command(mem4, 1, True, now)
    res = smx.merge_switches(
        mem4,
        smx.parse_switches(body({1: True, 2: True, 3: False, 4: True})),
        now + smx.QUARANTINE_SECONDS + 1,
    )
    check(
        res.merged[2] is True and not res.held,
        "after the deadline strip truth wins without confirmation",
    )

    # -- a second tap restarts the window ---------------------------------------
    mem5 = smx.StripMemory()
    smx.merge_switches(mem5, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    smx.note_command(mem5, 1, True, now)
    smx.merge_switches(mem5, smx.parse_switches(body({1: True, 2: True, 3: False, 4: True})), now + 1)
    smx.note_command(mem5, 2, False, now + 2)
    check(
        mem5.commanded == 2 and mem5.want is False and mem5.confirm == {},
        "a new command replaces the quarantine and its evidence",
    )
    check(
        abs(mem5.deadline - (now + 2 + smx.QUARANTINE_SECONDS)) < 1e-9,
        "a new command extends the deadline from its own moment",
    )

    # -- malformed bodies freeze, then escalate -----------------------------------
    mem6 = smx.StripMemory()
    smx.merge_switches(mem6, smx.parse_switches(body({1: False, 2: False, 3: False, 4: True})), now)
    mem6.last_body = body({1: False, 2: False, 3: False, 4: True})
    res = smx.merge_switches(mem6, None, now + 1)
    check(
        res.frozen and not res.dropped and res.merged == {1: False, 2: False, 3: False, 4: True},
        "a lone malformed body freezes the last frame, available",
    )
    smx.merge_switches(mem6, None, now + 6)
    res = smx.merge_switches(mem6, None, now + 11)
    check(
        res.dropped,
        "a persistently unreadable strip escalates to no-reading",
    )
    res = smx.merge_switches(
        smx.StripMemory(),
        None,
        now,
    )
    check(
        res.dropped,
        "a malformed first poll with nothing to freeze drops",
    )
    # recovery
    res = smx.merge_switches(mem6, smx.parse_switches(body({1: True, 2: False, 3: False, 4: True})), now + 16)
    check(
        not res.dropped and not res.frozen and res.merged[1] is True,
        "a valid poll after rot recovers immediately",
    )

    # -- strips do not share memory ----------------------------------------------
    mema = smx.StripMemory()
    memb = smx.StripMemory()
    smx.merge_switches(mema, smx.parse_switches(body({1: True, 2: True, 3: True, 4: True})), now)
    smx.merge_switches(memb, smx.parse_switches(body({1: False, 2: False, 3: False, 4: False})), now)
    smx.note_command(mema, 1, False, now)
    res = smx.merge_switches(mema, smx.parse_switches(body({1: True, 2: False, 3: True, 4: True})), now + 1)
    check(
        res.merged == {1: True, 2: True, 3: True, 4: True} and res.held == (2,),
        "quarantine evidence is per strip",
    )
    check(
        memb.good == {1: False, 2: False, 3: False, 4: False} and memb.commanded is None,
        "a command on one strip touches no other strip",
    )

    print(f"\n{passed} passed, {len(failed)} failed")
    for label in failed:
        print(f"  FAIL {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
