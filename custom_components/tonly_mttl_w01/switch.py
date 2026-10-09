"""Switch platform for MTTL-W01 WiFi.

Read the note at the top of `api.py` before changing anything here. The wrong
route from this file is not a theoretical mistake: an earlier version of this
integration keyed four places on the controller's `id`, which is the firmware
channel, and posted to the channel route. Every entity was labelled with a socket
number while driving a different one, and on three of four outlets the label was
simply a lie. The server survived by luck, because the controller refused OFF on
the protected channel.

So, stated as rules this module does not break:

1. Entities are keyed on `socket`, the PHYSICAL number. Never on `id`/`channel`.
2. Writes go through `MttlW01Api.set_socket`, which only accepts 1-4.
3. A protected outlet refuses OFF here, locally, before anything is sent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MttlW01ProtectedError
from .const import (
    ATTR_DRAWS_CURRENT,
    ATTR_PROTECTED,
    ATTR_REACHABLE,
    ATTR_SIMULATED,
    ATTR_SOCKET,
    DOMAIN,
    SOCKET_COUNT,
)
from .coordinator import MttlW01StateCoordinator, validate_scan_interval

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switches for every strip this controller knows about."""
    runtime = entry.runtime_data
    coordinator: MttlW01StateCoordinator = runtime.state
    created: set[str] = set()

    async def _add(devids: list[str]) -> None:
        fresh = [devid for devid in devids if devid not in created]
        if not fresh:
            return
        created.update(fresh)
        async_add_entities(
            [
                MttlW01Switch(coordinator, devid, number)
                for devid in fresh
                for number in range(1, SOCKET_COUNT + 1)
            ]
        )

    entry.async_on_unload(coordinator.async_register_platform(_add))
    await _add(coordinator.known_strips)


class MttlW01Switch(CoordinatorEntity[MttlW01StateCoordinator], SwitchEntity):
    """One physical outlet of one strip.

    `devid` and `number` are the whole identity: a strip id and a PHYSICAL socket
    number. The firmware channel is not stored on this entity at all, so it
    cannot leak into an entity id, a unique_id or a name by accident.
    """

    _attr_has_entity_name = False
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coordinator: MttlW01StateCoordinator,
        devid: str,
        number: int,
    ) -> None:
        super().__init__(coordinator)
        self._devid = devid
        self._number = number

        doc = coordinator.device_doc(devid) or {}
        model = str(doc.get("model") or "MTTL-W01")
        self._attr_unique_id = f"{devid}_socket_{number}"
        self._attr_name = self._build_name(coordinator, number)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, devid)},
            "manufacturer": "TONLY / LG-U+",
            "model": model,
            "name": self._strip_name(doc, devid),
            "sw_version": str(doc.get("firmware") or ""),
        }
        # Handle of the pending post-command re-read, if any. A second tap
        # cancels the first tap's refresh: overlapping echo tasks would only
        # re-read the same settling strip twice.
        self._echo_task: asyncio.Task[None] | None = None

    # -- naming ------------------------------------------------------

    @staticmethod
    def _strip_name(doc: dict[str, Any], devid: str) -> str:
        if doc.get("simulated") or devid == "SIM":
            return "MTTL-W01 (simulator)"
        model = str(doc.get("model") or "MTTL-W01")
        return f"{model} {devid}"

    @staticmethod
    def _is_default_name(name: str | None, number: int) -> bool:
        """True for the controller's untouched default outlet name.

        The controller's default is the literal English string "Socket N".
        Treating it as absent is what stops an English fragment being spliced
        into the middle of a translated interface - a bug that shipped once in
        this project's Android app and could only be seen by rendering the
        screen in that language.
        """
        if not name:
            return True
        return name.strip().lower() == f"socket {number}".strip()

    def _build_name(self, coordinator: MttlW01StateCoordinator, number: int) -> str:
        entry = coordinator.socket(self._devid, number)
        name = entry.get("name") if entry else None
        if self._is_default_name(name, number):
            # A neutral, translatable-by-nature label. The word is the only
            # translatable part; the number is the same in every language.
            return f"Socket {number}"
        return str(name)

    # -- state -------------------------------------------------------

    @property
    def _entry(self) -> dict[str, Any] | None:
        return self.coordinator.socket(self._devid, self._number)

    @property
    def _is_protected(self) -> bool:
        entry = self._entry
        if entry is not None:
            return bool(entry.get(ATTR_PROTECTED))
        # No reading right now. Fall back to the strip's configured locks, which
        # come from saved config rather than a live read and are therefore still
        # trustworthy when the strip is asleep.
        return self.coordinator.is_protected(self._devid, self._number)

    @property
    def available(self) -> bool:
        """Whether this entity currently has a meaningful value.

        Four conditions, and each exists for a specific reason:

        - coordinator healthy: the controller itself is answering;
        - a REAL strip: the simulator is not presented as the user's hardware;
        - settled: the post-restart all-zero block is not read as truth;
        - reachable: this particular strip is answering.
        """
        if not self.coordinator.last_update_success:
            return False
        if not self.coordinator.is_real_strip(self._devid):
            return False
        if not self.coordinator.is_settled(self._devid):
            return False
        if not self.coordinator.is_reachable(self._devid):
            return False
        return self._entry is not None

    @property
    def is_on(self) -> bool:
        entry = self._entry
        return bool(entry.get("on")) if entry else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry = self._entry or {}
        attrs: dict[str, Any] = {
            ATTR_SOCKET: self._number,
            ATTR_PROTECTED: self._is_protected,
            ATTR_REACHABLE: bool(entry.get(ATTR_REACHABLE, False)),
        }
        if entry:
            attrs[ATTR_DRAWS_CURRENT] = bool(entry.get(ATTR_DRAWS_CURRENT))
        return attrs

    # -- writes ------------------------------------------------------

    async def _async_drive(self, on: bool) -> None:
        if not on and self._is_protected:
            # Refused here, not sent and rejected later. The controller would
            # answer 409 with the same information, but a user tapping a switch
            # should get the reason immediately rather than a failed command
            # that appears to have been sent.
            raise HomeAssistantError(
                f"Socket {self._number} is protected: this outlet cannot be "
                "switched off, whatever asks. It can still be switched back ON to "
                "restore power. Remove it from the controller's protection list "
                "if that is not what you want."
            )

        # Open the settling window BEFORE the POST, not after it succeeds: a
        # poll landing in the round-trip gap would otherwise serve pre-tap
        # truth and visibly revert the tap. If the command is refused or
        # fails below, the window is released again at once.
        self.coordinator.command_started(self._devid, self._number, on)
        try:
            await self.coordinator.api.set_socket(
                self._number, on, device=self._devid
            )
        except MttlW01ProtectedError as err:
            # The controller's own wording leads the message. It names the
            # physical socket and says why, which is exactly what a generic
            # "failed" would throw away.
            self.coordinator.command_failed(self._devid)
            self._schedule_echo_refresh()
            raise HomeAssistantError(str(err)) from err
        except Exception as err:  # noqa: BLE001
            # The tap may or may not have landed - a timeout after the strip
            # applied is the classic case. The window is released, because
            # there is no known transition to settle, but one prompt re-read
            # still resyncs the UI instead of sitting stale until the next
            # scheduled poll.
            self.coordinator.command_failed(self._devid)
            self._schedule_echo_refresh()
            raise HomeAssistantError(
                f"Could not switch socket {self._number}: {err}"
            ) from err

        # The command landed and the window is already open, so the tapped
        # socket shows the tapped value at once (see below) while the other
        # sockets of this strip hold last-good - and then re-read once the
        # strip's echo has landed and the controller's cache has expired.
        #
        # The tapped value displays immediately, deliberately: a pre-echo
        # poll would otherwise revert the tap in the UI for a cycle - the
        # switch visibly bouncing off-on-off on a single tap. If the echo
        # never confirms (the strip dropped mid-command), the window expires
        # and strip truth wins again, so the most this can mislead by is a
        # few seconds on a command whose fate is genuinely unknown.
        #
        # A refresh right now would still lie about everything else: the
        # controller force-reads state immediately after sending the command,
        # *before* the strip's echo lands, and serves that stale reading
        # from its poll-window cache for the next couple of seconds. So the
        # service call returns immediately and a background task re-reads
        # ~2.5s later, once the cache has expired, so the UI shows the
        # strip's own answer after a couple of seconds instead of on the
        # next scheduled poll.
        self._schedule_echo_refresh()

    def _schedule_echo_refresh(self) -> None:
        """Schedule the post-command re-read, cancelling any older one.

        A second tap supersedes the first tap's refresh: the older task was
        going to re-read a strip that has since been commanded again, so its
        result would only add load, never information.
        """
        old = self._echo_task
        if old is not None and not old.done():
            old.cancel()
        self._echo_task = self.hass.async_create_task(
            self._async_refresh_after_echo()
        )

    async def _async_refresh_after_echo(self) -> None:
        """Re-read after the strip's echo has landed and the cache expired.

        This must be `async_refresh`, not `async_request_refresh`: the latter
        is debounced by the coordinator (a 10s cooldown window in current Home
        Assistant), so a request two seconds after a command would be batched
        and swallowed. `async_refresh` runs now, and the controller's poll
        cache has expired by then, so it reads the strip's own answer.
        """
        try:
            await asyncio.sleep(1.5)
            await self.coordinator.async_refresh()
        except asyncio.CancelledError:
            # Unload during the wait: nothing to refresh, nothing to clean up.
            return

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_drive(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_drive(False)


__all__ = ["MttlW01Switch", "async_setup_entry"]