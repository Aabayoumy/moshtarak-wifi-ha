"""The MTTL-W01 WiFi integration.

Controls TONLY / LG-U+ MTTL-W01 four-socket Wi-Fi power strips in Home Assistant.

Shape of the thing, and why:

    MTTL-W01 strip  --dials out to TCP 10086-->  app/add-on (the controller)
                                                   |
                                                   | HTTP, internal only
                                                   v
                                            this integration

The strip never accepts an inbound connection - ports 10086, 30888, 80 and 8080
are all closed on it. The address it "reports" for itself is the source port it
dialled out from. So the controller has to be the listener, which is why the
protocol lives in the add-on and not here. This integration is an HTTP client
for that controller's REST API and contains no vendor protocol code at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MttlW01Api, MttlW01ApiError
from .const import (
    CONF_HOST,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_NO_REAL_STRIP,
    ISSUE_PROTECTION_UNKNOWN,
)
from .coordinator import (
    MttlW01ProbeCoordinator,
    MttlW01StateCoordinator,
    validate_scan_interval,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


@dataclass
class MttlW01RuntimeData:
    """Everything a platform needs, attached to the config entry."""

    api: MttlW01Api
    state: MttlW01StateCoordinator
    probe: MttlW01ProbeCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MTTL-W01 WiFi from a config entry."""
    host = str(entry.data.get(CONF_HOST) or "").strip().rstrip("/")
    if not host:
        _LOGGER.error("No controller address configured")
        return False

    api = MttlW01Api(async_get_clientsession(hass), host)

    try:
        health = await api.health()
    except MttlW01ApiError as err:
        # Retrying is right: the add-on may simply not be started yet, and a
        # first-time installer will hit this every time.
        raise ConfigEntryNotReady(
            f"The MTTL-W01 WiFi controller at {host} is not answering: {err}"
        ) from err

    if not health.get("ok"):
        raise ConfigEntryNotReady(f"The controller at {host} did not report ok")

    interval = validate_scan_interval(entry.options.get(CONF_SCAN_INTERVAL, 5))

    state = MttlW01StateCoordinator(hass, api, interval)
    probe = MttlW01ProbeCoordinator(hass, api, state)

    entry.runtime_data = MttlW01RuntimeData(api=api, state=state, probe=probe)

    # Refresh up front so the first `async_add_entities` already knows which
    # strips exist. Without this, a strip that is already connected would have
    # no entities until the first scheduled poll, and the dashboard would look
    # empty for a reason that has nothing to do with reality.
    try:
        await state.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(
            f"Could not read the initial state from {host}: {err}"
        ) from err

    # The probe coordinator is started here, not in the platforms: it is an
    # active query to the strip and has no entities to add. A failure to reach
    # it must NOT stop the integration loading - the switches still work, and
    # voltage simply has no reading yet.
    try:
        await probe.async_refresh()
    except MttlW01ApiError as err:
        _LOGGER.debug("No measurement available yet: %s", err)

    await _async_update_repairs(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(state.async_add_listener(_make_repair_listener(hass, entry)))

    _LOGGER.info(
        "MTTL-W01 WiFi ready: %s real strip(s) known to the controller at %s",
        len(state.real_strips()),
        host,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for issue_id in (ISSUE_NO_REAL_STRIP, ISSUE_PROTECTION_UNKNOWN):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
    return unloaded


def _make_repair_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Rebuild repair issues whenever the coordinator polls."""

    async def _listener() -> None:
        await _async_update_repairs(hass, entry)

    return _listener


async def _async_update_repairs(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create or clear the repair issues.

    Two issues, both about things a user cannot see and would otherwise assume
    are fine:

    1. The controller is answering but only its simulator is. The user would see
       a working-looking integration controlling nothing.
    2. Protection is configured for a device id the controller has never seen.
       Protection is keyed by device id, so an id with a typo in it silently
       protects nothing - and the symptom is an outlet holding a server that has
       quietly become switchable. The controller logs the same warning; this
       surfaces it where a user will actually meet it.

    Both are deliberately quiet when there is nothing wrong. A repair issue that
    fires on every poll is one nobody reads.
    """
    runtime: MttlW01RuntimeData = entry.runtime_data
    state = runtime.state

    if not state.data:
        return

    if not state.has_real_strip():
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_NO_REAL_STRIP,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_REAL_STRIP,
            translation_placeholders={
                "host": runtime.api.host,
                "known": ", ".join(state.known_strips) or "none",
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_NO_REAL_STRIP)

    known = set(state.known_strips)
    typo = [
        devid
        for devid in state.protected_by_device()
        if str(devid).upper() not in known
    ]
    if typo and known:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_PROTECTION_UNKNOWN,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_PROTECTION_UNKNOWN,
            translation_placeholders={"devices": ", ".join(sorted(typo))},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_PROTECTION_UNKNOWN)