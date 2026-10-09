"""Diagnostics support for MTTL-W01 WiFi.

Everything here is read-only. It reports what the controller says about itself,
plus the raw per-strip state, so a bug report can be answered without asking
someone to photograph a screen.

The socket-to-channel mapping appears here, in diagnostics, and nowhere else.
It is the one place it belongs: it is essential for debugging and actively
harmful in a user interface, because two numberings for one thing is the root
cause of this project's worst incidents.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MttlW01ApiError


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    api = runtime.api
    state = runtime.state

    out: dict[str, Any] = {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), ["host"]),
            "options": dict(entry.options),
        },
        "controller": {},
        "strips": {},
        "socket_to_channel_map": {
            "physical_socket_to_firmware_channel": state.order(),
            "note": (
                "Measured on real hardware. Physical socket N is firmware "
                "channel order[N-1]. Every user-facing surface uses socket "
                "numbers; this mapping is diagnostics only."
            ),
        },
        "known_strips": state.known_strips,
        "real_strips": state.real_strips(),
        "protection_by_device": state.protected_by_device(),
        "locks_by_socket": {
            devid: state.locks(devid) for devid in state.known_strips
        },
    }

    for label, coro in (
        ("health", api.health()),
        ("config", api.controller_config()),
        ("diagnostics", api.controller_diagnostics()),
        ("devices", api.devices()),
    ):
        try:
            out["controller"][label] = await coro
        except MttlW01ApiError as err:
            out["controller"][label] = {"error": str(err)}

    for devid in state.known_strips:
        strip: dict[str, Any] = {
            "is_simulated": not state.is_real_strip(devid),
            "settled": state.is_settled(devid),
            "reachable": state.is_reachable(devid),
            "protected_sockets": state.locks(devid),
            "state": state.strip(devid),
            "measurement": runtime.probe.measurement(devid),
        }
        out["strips"][devid] = strip

    return out


__all__ = ["async_get_config_entry_diagnostics"]