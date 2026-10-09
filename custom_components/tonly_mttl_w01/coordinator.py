"""Coordinators for MTTL-W01 WiFi.

Two of them, on purpose.

`MttlW01StateCoordinator` polls the controller's cached status read every few
seconds. That is cheap: the controller serves a cache, so several clients polling
do not each provoke a fresh read of the strip.

`MttlW01ProbeCoordinator` polls `/api/probe`, which sends an ACTIVE query to
the strip and is not served from a cache. Running it at the state cadence would
be needlessly chatty on the wire, so it runs on its own, much slower schedule.

## The trap this file exists to handle

With no real strip connected, the controller still answers `/api/state` with four
healthy sockets, `reachable: true` and `settled: true`. It is not lying - it is
answering from its own in-process simulator, which reports as device `SIM`.

So neither flag is enough to conclude that a user's power strip is answering.
`settled` is true for the simulator; `reachable` is true for the simulator. An
integration that trusted them would show four working-looking switches that
control nothing, which is the exact failure this project calls a facade.

Every availability decision here therefore asks whether a REAL strip answered.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MttlW01Api,
    MttlW01ApiError,
    MttlW01CannotConnect,
    MttlW01StripUnreachable,
)
from .const import (
    DOMAIN,
    MIN_SCAN_INTERVAL,
    PROBE_INTERVAL,
    SIMULATED_DEVICE_ID,
)
from .state_merge import (
    StripMemory,
    merge_switches,
    note_command,
    parse_switches,
)

_LOGGER = logging.getLogger(__name__)

PlatformAdder = Callable[[list[str]], Awaitable[None]]


class MttlW01StateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the per-strip state and the list of known strips."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MttlW01Api,
        scan_interval,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
        )
        self.api = api
        self._platforms: list[PlatformAdder] = []
        self._known: list[str] = []
        # Per-strip merge memory for the settling policy in state_merge.py:
        # last-good socket states, post-command quarantines, malformed-body
        # streaks. Plain data; it dies with the coordinator on reload, which
        # is correct - a fresh setup trusts the first valid poll outright.
        self._merge: dict[str, StripMemory] = {}

    # -- platform registration --------------------------------------

    def async_register_platform(self, adder: PlatformAdder) -> Callable[[], None]:
        """Register a platform's entity adder, and get an unsubscriber back.

        A strip that appears months after setup still has to get its entities,
        so platforms cannot simply build a list once and be done.
        """
        self._platforms.append(adder)

        def _remove() -> None:
            if adder in self._platforms:
                self._platforms.remove(adder)

        return _remove

    @property
    def known_strips(self) -> list[str]:
        """Device ids discovered so far, in stable order."""
        return list(self._known)

    # -- helpers used by the platforms -------------------------------

    @property
    def states(self) -> dict[str, dict[str, Any] | None]:
        if not isinstance(self.data, dict):
            return {}
        return self.data.get("states") or {}

    def strip(self, devid: str) -> dict[str, Any] | None:
        """The state body for one strip, or None if it could not be read."""
        return self.states.get(devid)

    def socket(self, devid: str, number: int) -> dict[str, Any] | None:
        """One outlet of one strip, by PHYSICAL socket number.

        Returns None rather than a default dict. A socket that is missing is not
        an outlet that is off - it is an outlet we have no reading for, and
        conflating those is how an empty list comes to mean "nothing is there".
        """
        body = self.strip(devid)
        if not body:
            return None
        for entry in body.get("switches") or []:
            if entry.get("socket") == number:
                return entry
        return None

    def is_simulated(self, devid: str) -> bool:
        """True when this device id is the controller's built-in simulator."""
        if devid == SIMULATED_DEVICE_ID:
            return True
        body = self.strip(devid)
        if body and body.get("simulated"):
            return True
        for dev in self._devices():
            if dev.get("devid") == devid and dev.get("simulated"):
                return True
        return False

    def is_real_strip(self, devid: str) -> bool:
        return not self.is_simulated(devid)

    def real_strips(self) -> list[str]:
        return [d for d in self._known if self.is_real_strip(d)]

    def has_real_strip(self) -> bool:
        return bool(self.real_strips())

    def is_settled(self, devid: str) -> bool:
        """False in the short window after a controller restart.

        The controller answers `/api/state` with all four sockets off and raw 0
        before the strip has re-dialled and reported - including for an outlet
        that is very much powered. The relays do NOT open. Reading that as truth
        would show a running server as switched off.

        Note this flag is ALSO true for the simulator, which is why
        `is_real_strip` has to be checked separately.
        """
        body = self.strip(devid)
        if body is None:
            return False
        return bool(body.get("settled"))

    def is_reachable(self, devid: str) -> bool:
        body = self.strip(devid)
        if body is None:
            return False
        return bool(body.get("reachable"))

    def locks(self, devid: str) -> list[int]:
        """Protected PHYSICAL socket numbers for a strip.

        Returns sockets, not firmware channels, because a socket number is
        something a user can match to the socket in front of them.
        """
        body = self.strip(devid)
        if body:
            protection = body.get("protection") or {}
            if protection.get("device") in (devid, "", None):
                sockets = protection.get("sockets")
                if isinstance(sockets, list):
                    return [s for s in sockets if isinstance(s, int)]
        for dev in self._devices():
            if dev.get("devid") == devid:
                locks = dev.get("protected_sockets")
                if isinstance(locks, list):
                    return [s for s in locks if isinstance(s, int)]
        return []

    def is_protected(self, devid: str, number: int) -> bool:
        return number in self.locks(devid)

    def protected_by_device(self) -> dict[str, Any]:
        if isinstance(self.data, dict):
            return self.data.get("protection_by_device") or {}
        return {}

    def order(self) -> list[int]:
        if isinstance(self.data, dict):
            order = self.data.get("order")
            if isinstance(order, list) and len(order) == 4:
                return order
        return []

    def _devices(self) -> list[dict[str, Any]]:
        if not isinstance(self.data, dict):
            return []
        return self.data.get("devices") or []

    def device_doc(self, devid: str) -> dict[str, Any] | None:
        for dev in self._devices():
            if dev.get("devid") == devid:
                return dev
        return None

    def command_sent(self, devid: str, socket: int, want: bool) -> None:
        """Record a successful switch command for the settling policy.

        Called only after the controller accepted the command - never for a
        refused or failed one, where there is no transition to settle. The
        commanded socket keeps rendering strip truth; the other sockets of
        this strip hold last-good until confirmed. See state_merge.py.
        """
        memory = self._merge.setdefault(
            str(devid).strip().upper(), StripMemory()
        )
        note_command(memory, socket, want, time.monotonic())

    def _merge_states(
        self, states: dict[str, dict[str, Any] | None]
    ) -> dict[str, dict[str, Any] | None]:
        """Fold one poll's strip bodies through the settling policy.

        Bodies that are not even well-shaped carry no information: the last
        good frame is reused briefly, and only persistent rot reads as
        unavailable. Well-shaped bodies have their socket states merged
        against post-command quarantines. Everything else in the body -
        power, telemetry, protection - flows through untouched.
        """
        now = time.monotonic()
        for devid, body in states.items():
            if body is None:
                # Transport-level failure for this strip: the existing
                # unavailable path, unchanged. Memory is left alone; wall
                # clock expires any quarantine on its own.
                continue
            memory = self._merge.setdefault(
                str(devid).strip().upper(), StripMemory()
            )
            reported = parse_switches(body)
            result = merge_switches(memory, reported, now)
            if result.dropped:
                states[devid] = None
                _LOGGER.debug(
                    "Strip %s unreadable, marking unavailable", devid
                )
            elif result.frozen:
                states[devid] = memory.last_body
                _LOGGER.debug(
                    "Strip %s sent a malformed block, freezing last frame",
                    devid,
                )
            else:
                for entry in body.get("switches", []):
                    number = entry.get("socket") if isinstance(entry, dict) else None
                    if isinstance(number, int) and number in result.merged:
                        entry["on"] = result.merged[number]
                memory.last_body = body
                if result.held:
                    _LOGGER.debug(
                        "Holding socket(s) %s of strip %s at last-good "
                        "until confirmed",
                        sorted(result.held),
                        devid,
                    )
        return states

    # -- the update --------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            devices_doc = await self.api.devices()
        except MttlW01CannotConnect as err:
            raise UpdateFailed(str(err)) from err
        except MttlW01ApiError as err:
            raise UpdateFailed(f"Unexpected controller error: {err}") from err

        devices = devices_doc.get("devices") or []

        # Real strips first, simulator last, so a strip is never hidden behind
        # the thing standing in for it.
        ordered: list[str] = []
        for dev in devices:
            devid = str(dev.get("devid") or "").strip().upper()
            if not devid or devid in ordered:
                continue
            if not dev.get("simulated"):
                ordered.append(devid)
        simulator_present = any(
            str(d.get("devid") or "").strip().upper() == SIMULATED_DEVICE_ID
            or d.get("simulated")
            for d in devices
        )
        if simulator_present and SIMULATED_DEVICE_ID not in ordered:
            ordered.append(SIMULATED_DEVICE_ID)

        states: dict[str, dict[str, Any] | None] = {}
        for devid in ordered:
            try:
                if devid == SIMULATED_DEVICE_ID:
                    states[devid] = await self.api.state()
                else:
                    states[devid] = await self.api.state(devid)
            except MttlW01ApiError as err:
                # The controller is up; this one strip is not answering. Recorded
                # as "no reading" for that strip only. Not a failure of the
                # whole integration, and definitely not a reason to stop polling
                # everyone else.
                _LOGGER.debug("Strip %s did not answer: %s", devid, err)
                states[devid] = None

        payload: dict[str, Any] = {
            "states": self._merge_states(states),
            "devices": devices,
            "protection_by_device": devices_doc.get("protection_by_device") or {},
            "selected": devices_doc.get("selected") or "",
            "listener": devices_doc.get("listener") or "",
            "order": [],
        }

        # `order` lives on the per-strip state body, not on /api/devices.
        for body in states.values():
            if body and isinstance(body.get("order"), list) and len(body["order"]) == 4:
                payload["order"] = body["order"]
                break

        new_strips = [d for d in ordered if d not in self._known]
        if new_strips:
            self._known = ordered
            for adder in list(self._platforms):
                try:
                    await adder(new_strips)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error adding entities for new strip(s) %s", new_strips)
        else:
            self._known = ordered

        return payload


class MttlW01ProbeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Volts and RSSI, on a slow cadence, per strip."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MttlW01Api,
        state: MttlW01StateCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_probe",
            update_interval=PROBE_INTERVAL,
        )
        self.api = api
        self.state = state

    async def _async_update_data(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for devid in self.state.known_strips:
            try:
                out[devid] = await self.api.probe(devid)
            except MttlW01StripUnreachable as err:
                # The strip is asleep or rebooting. Ordinary. The sensors read
                # this as "no measurement", not as an error, because voltage
                # does not exist while the strip is not answering.
                _LOGGER.debug("No measurement from strip %s: %s", devid, err)
                out[devid] = None
            except MttlW01ApiError as err:
                raise UpdateFailed(str(err)) from err
        return out

    def measurement(self, devid: str) -> dict[str, Any] | None:
        if not isinstance(self.data, dict):
            return None
        return self.data.get(devid)


def validate_scan_interval(value: Any) -> timedelta | None:
    """Clamp a configured interval into something sane.

    Zero or negative would mean "poll as fast as possible against a device that
    answers on a timer", so it is floored rather than trusted.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(MIN_SCAN_INTERVAL, timedelta(seconds=float(value)))


__all__ = [
    "MttlW01ProbeCoordinator",
    "MttlW01StateCoordinator",
    "validate_scan_interval",
]