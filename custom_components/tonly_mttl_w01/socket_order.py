"""Recorded socket order, and the guard that watches it.

The controller's CONFIG["order"] maps physical sockets to firmware
channels, and everything - the app, timers, protection, this integration -
follows it. But that file lives outside the add-on's auto-persisted data
dir: an add-on update or reinstall silently reverts it to the built-in
default. On a strip whose measured order differs from the default, every
label would then drive the wrong outlet with no warning at all.

So the measured order is recorded a second time, somewhere an add-on
update cannot reach: `<config>/tonly_mttl_w01_socket_order.json`, written
once by hand when the strip is commissioned. Every poll compares what the
controller reports against it; on mismatch the integration raises a
repairs issue instead of silently driving the wrong relays.

No file, or an unreadable one, means no guard - fail open, documented.
Nothing here is imported from Home Assistant: parsing and loading are
exercised by tests/test_socket_order.py with stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

EXPECTED_ORDER_FILENAME = "tonly_mttl_w01_socket_order.json"
SOCKETS: tuple[int, ...] = (1, 2, 3, 4)


def parse_order(value: Any) -> Union[list[int], None]:
    """A permutation of 1..4, or None for anything else.

    Strict on purpose: a wrong-length list, a duplicate, an out-of-range
    channel, a boolean masquerading as 0/1, or a non-list is not "close
    enough" when the consequence is driving the wrong outlet.
    """
    if not isinstance(value, list) or len(value) != len(SOCKETS):
        return None
    if any(
        not isinstance(x, int) or isinstance(x, bool) or x not in SOCKETS
        for x in value
    ):
        return None
    if sorted(value) != list(SOCKETS):
        return None
    return list(value)


def load_expected_order(config_dir: Union[str, Path]) -> Union[list[int], None]:
    """Read the recorded order, or None when absent or unreadable.

    Fail open: a missing file simply means the guard is not armed. Never
    raises.
    """
    try:
        raw = (Path(config_dir) / EXPECTED_ORDER_FILENAME).read_text()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        return parse_order(parsed.get("order"))
    except Exception:
        return None
