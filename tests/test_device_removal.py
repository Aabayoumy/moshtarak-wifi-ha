#!/usr/bin/env python3
"""Test device_removal without a Home Assistant install.

The Delete button on a device is gated by `async_remove_config_entry_device`,
and the gate has two failure modes that reading the code does not catch:

* a rename of the hook - Home Assistant finds it with `hasattr`, so a typo
  would not crash anything, it would silently turn the feature off again;
* a policy that lets a live strip be deleted, or refuses to delete a
  leftover forever.

Only the stdlib and this one module are needed. `device_removal.py` imports
nothing from Home Assistant - the config entry and registry entry it would
need exist only inside a running Core - so the decision can be exercised
directly, by path, without importing the package (which would pull in
`__init__.py` and `homeassistant.config_entries` with it).
"""

from __future__ import annotations

import importlib.util  # noqa: E402
import re
import sys
from pathlib import Path  # noqa: E402

_MODULE_DIR = Path(__file__).resolve().parent.parent / "custom_components/tonly_mttl_w01"

_spec = importlib.util.spec_from_file_location(
    "_tonly_device_removal", _MODULE_DIR / "device_removal.py"
)
assert _spec and _spec.loader
drx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drx)

passed = 0
failed: list[str] = []

# The real strip currently connected, a strip id seen during earlier testing
# that is no longer reported by anything, and the controller's simulator.
LIVE = "D8AA59D270AA"
STALE = "2CFDB3355BA3"
SIM = "SIM"


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed.append(f"{label}: {detail}")
        print(f"  FAIL {label}  -- {detail}")


def main() -> int:
    print("device_removal.removal_allowed")

    # -- the controller says what is live ------------------------------
    check(
        drx.removal_allowed([LIVE, SIM], [LIVE]) is False,
        "a device the controller reports cannot be deleted",
        "live strip was deletable",
    )
    check(
        drx.removal_allowed([LIVE, SIM], [SIM]) is False,
        "the simulator cannot be deleted while it is being reported",
        "actively reported simulator was deletable",
    )
    check(
        drx.removal_allowed([LIVE], [STALE]) is True,
        "a leftover strip the controller stopped reporting can be deleted",
        "stale strip was not deletable",
    )
    check(
        drx.removal_allowed([LIVE], [SIM]) is True,
        "the simulator can be deleted once it is no longer reported",
        "stale simulator was not deletable",
    )
    check(
        drx.removal_allowed([LIVE], [STALE, LIVE]) is False,
        "one live identifier among several is enough to refuse",
        "device with a live identifier slipped through",
    )

    # -- casing and padding must not open a gap ------------------------
    check(
        drx.removal_allowed([LIVE.lower()], [LIVE]) is False,
        "matching is case-insensitive",
        "lowercase known id did not match",
    )
    check(
        drx.removal_allowed([f" {LIVE} "], [LIVE]) is False,
        "matching tolerates stray whitespace",
        "padded known id did not match",
    )

    # -- nobody can say -------------------------------------------------
    check(
        drx.removal_allowed(None, [LIVE]) is False,
        "no successful poll -> the live strip stays protected",
        "unknown state allowed deleting the live strip",
    )
    check(
        drx.removal_allowed(None, [STALE]) is False,
        "no successful poll -> nothing is deleted at all",
        "unknown state allowed a delete",
    )
    check(
        drx.removal_allowed([], [STALE]) is True,
        "controller up but listing nothing -> leftovers are deletable",
        "empty device list refused a leftover",
    )
    check(
        drx.removal_allowed([], []) is True,
        "a device with no identifiers of ours has nothing to protect",
        "empty identifier set was refused",
    )

    # -- the hook itself exists under the exact name HA discovers -------
    init_src = (_MODULE_DIR / "__init__.py").read_text()
    match = re.search(
        r"async def async_remove_config_entry_device\(\s*"
        r"(?P<args>[^)]*)\)",
        init_src,
    )
    check(match is not None, "__init__.py defines async_remove_config_entry_device")
    if match:
        args = match.group("args")
        check(
            all(name in args for name in ("hass", "config_entry", "device_entry")),
            "the hook keeps Home Assistant's three positional parameters",
            f"signature is ({args})",
        )

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        print("\nFAILED:")
        for f in failed:
            print(f"  - {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
