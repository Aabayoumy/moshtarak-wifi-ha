#!/usr/bin/env python3
"""Test socket_order without a Home Assistant install.

The controller's socket order lives outside the add-on's persisted data
dir, so an update or reinstall silently reverts it to the built-in
default. On a strip whose measured order differs, every label would then
drive the wrong outlet with no warning. The guard is a recorded copy in
the config dir plus a per-poll comparison; this pins down the parts that
can run without Home Assistant:

* only a true permutation of 1..4 counts - duplicates, gaps, booleans,
  strings, wrong lengths and non-lists are all refused;
* a missing, unreadable, or malformed file disables the guard instead of
  crashing it (fail open, so a deleted file can never break polling).

Only the stdlib and this one module are needed. `socket_order.py` imports
nothing from Home Assistant, so the parsing and loading are exercised
directly, by path, without importing the package.
"""

from __future__ import annotations

import importlib.util  # noqa: E402
import sys
import tempfile
from pathlib import Path  # noqa: E402

_MODULE_DIR = Path(__file__).resolve().parent.parent / "custom_components/tonly_mttl_w01"

_spec = importlib.util.spec_from_file_location(
    "_tonly_socket_order", _MODULE_DIR / "socket_order.py"
)
assert _spec and _spec.loader
sox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sox)

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


def write_tmp(tmp: str, name: str, content: str) -> str:
    path = Path(tmp) / name
    path.write_text(content)
    return tmp


def main() -> int:
    print("socket_order.parse_order / load_expected_order")

    # -- parsing: only a true permutation counts ----------------------
    check(
        sox.parse_order([1, 2, 3, 4]) == [1, 2, 3, 4],
        "identity order parses",
    )
    check(
        sox.parse_order([2, 3, 4, 1]) == [2, 3, 4, 1],
        "a rotated order is still a valid permutation",
    )
    check(
        sox.parse_order([1, 1, 2, 3]) is None,
        "duplicates are refused",
    )
    check(
        sox.parse_order([0, 1, 2, 3]) is None,
        "out-of-range channels are refused",
    )
    check(
        sox.parse_order([1, 2, 3]) is None,
        "a short list is refused",
    )
    check(
        sox.parse_order([1, 2, 3, 4, 1]) is None,
        "a long list is refused",
    )
    check(
        sox.parse_order("1234") is None,
        "a string is refused",
    )
    check(
        sox.parse_order(None) is None,
        "nothing is refused",
    )
    check(
        sox.parse_order({"order": [1, 2, 3, 4]}) is None,
        "a dict is refused",
    )
    check(
        sox.parse_order([True, 2, 3, 4]) is None,
        "booleans are refused even though True == 1",
    )
    check(
        sox.parse_order(["1", "2", "3", "4"]) is None,
        "strings are refused even when numeric",
    )

    # -- loading: fail open, never raise -------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        check(
            sox.load_expected_order(tmp) is None,
            "a missing file disables the guard",
        )
        write_tmp(tmp, sox.EXPECTED_ORDER_FILENAME, '{"order": [1, 2, 3, 4]}')
        check(
            sox.load_expected_order(tmp) == [1, 2, 3, 4],
            "a recorded order loads",
        )
        check(
            sox.load_expected_order(Path(tmp)) == [1, 2, 3, 4],
            "a Path config dir works too",
        )
        write_tmp(tmp, sox.EXPECTED_ORDER_FILENAME, "not json at all")
        check(
            sox.load_expected_order(tmp) is None,
            "unparseable content disables the guard",
        )
        write_tmp(tmp, sox.EXPECTED_ORDER_FILENAME, "[1, 2, 3, 4]")
        check(
            sox.load_expected_order(tmp) is None,
            "a non-object root disables the guard",
        )
        write_tmp(tmp, sox.EXPECTED_ORDER_FILENAME, '{"nope": 1}')
        check(
            sox.load_expected_order(tmp) is None,
            "a missing order key disables the guard",
        )
        write_tmp(tmp, sox.EXPECTED_ORDER_FILENAME, '{"order": [1, 1, 2, 3]}')
        check(
            sox.load_expected_order(tmp) is None,
            "an invalid recorded order disables the guard",
        )

    print(f"\n{passed} passed, {len(failed)} failed")
    for label in failed:
        print(f"  FAIL {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
