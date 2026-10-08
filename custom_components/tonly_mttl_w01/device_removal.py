"""May Home Assistant delete this device? The decision, kept testable.

Home Assistant asks before it removes a device from its registry: the
integration's `async_remove_config_entry_device` (in `__init__.py`) answers
yes or no. That hook needs a config entry and a registry entry, neither of
which exists on a machine without Home Assistant installed - so the actual
question lives here as plain data, the same way `host_discovery.py` keeps
slug matching away from the Supervisor.

The question is "does the controller still report this device id?", and the
answers differ in a way that matters:

* **It does** -> refuse. Home Assistant deletes a device's entities along
  with the device, so deleting a live strip would take its working switches
  with it - and the very next poll would recreate them, because the strip is
  still there. The user would watch a delete appear to succeed and then undo
  itself, which is worse than a delete that never worked.
* **It does not** -> allow. That device is a leftover: a strip that was
  unplugged and never came back, or the simulator from a setup where it was
  the only thing running. Its entities are already dark, and the controller
  will never report it again. This is exactly what the Delete button is for.
* **Nobody can say** - no successful poll to compare against, because the
  entry is setting up or in error - -> refuse. Acting on missing data would
  be the same trap this project already refuses elsewhere: `coordinator.py`
  documents at length why a state that cannot tell the simulator apart from
  real hardware must never be trusted to make things happen.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def removal_allowed(
    known_strips: Sequence[str] | None,
    device_ids: Iterable[str],
) -> bool:
    """Return True when a device carrying `device_ids` may be deleted.

    `known_strips` is what the controller last reported, or None when there
    is no successful reading to trust. `device_ids` are the ids claimed by
    the device being deleted - only those in this integration's own domain
    are passed in, see `__init__.py`.

    Comparison is case- and whitespace-insensitive on purpose: ids arrive
    uppercased from the coordinator, but a device registry entry written by
    an older version, or by hand, must not slip through a casing difference
    and make a live strip deletable.
    """
    if known_strips is None:
        return False
    known = {str(devid).strip().upper() for devid in known_strips}
    return all(
        str(devid).strip().upper() not in known for devid in device_ids
    )
