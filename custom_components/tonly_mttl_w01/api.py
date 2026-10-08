"""Async HTTP client for the MTTL-W01 WiFi controller.

This module contains NO vendor protocol code. It speaks only the controller's
own REST API, which is why the integration carries none of the licensing weight
of the protocol implementation in the app/add-on.

The single most important rule in this file:

    switch an outlet with POST /api/switch/socket/<PHYSICAL SOCKET NUMBER>

The controller also exposes POST /api/switch/<firmware channel>, labelled
"legacy, used by HA" in its own source. Using that route from HA is a real bug
that shipped once and drove three of four outlets to the wrong socket, one of
which was the server. `set_socket` is the only write method this module exposes,
and it takes a physical socket number. There is deliberately no `set_channel`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import ATTR_CHANNEL, ATTR_PROTECTED, ATTR_SOCKET

DEFAULT_TIMEOUT = 10


class MttlW01ApiError(Exception):
    """Base error for the controller API."""


class MttlW01CannotConnect(MttlW01ApiError):
    """The controller could not be reached at all."""


class MttlW01InvalidResponse(MttlW01ApiError):
    """The controller answered with something this client cannot trust."""


class MttlW01ProtectedError(MttlW01ApiError):
    """The controller refused because the outlet is protected.

    The controller's own wording is carried through verbatim, e.g.
    "firmware channel 3 (physical socket 2) is protected and will not be
    switched off". Replacing it with a generic message would throw away the one
    piece of information that tells a user exactly which outlet is locked and why.
    """


class MttlW01StripUnreachable(MttlW01ApiError):
    """The controller is fine but the strip itself cannot be reached.

    A normal state, not a broken integration: the strip is rebooting, asleep, or
    not provisioned yet. Kept distinct from MttlW01CannotConnect on purpose,
    because collapsing the two would report a perfectly healthy controller as a
    dead one and hide the actual problem.
    """


class MttlW01Api:
    """Thin async wrapper over the controller's HTTP API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self.host = host.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _url(self, path: str) -> str:
        return f"{self.host}{path}"

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            async with self._session.get(
                self._url(path), timeout=self._timeout
            ) as resp:
                body = await resp.json(content_type=None)
        except asyncio.TimeoutError as err:
            raise MttlW01CannotConnect(
                f"Timed out talking to the controller at {self.host}"
            ) from err
        except aiohttp.ClientError as err:
            raise MttlW01CannotConnect(
                f"Cannot reach the controller at {self.host}: {err}"
            ) from err
        except ValueError as err:
            raise MttlW01InvalidResponse(
                f"The controller at {self.host} did not return JSON"
            ) from err

        if not isinstance(body, dict):
            raise MttlW01InvalidResponse(
                f"Expected a JSON object from {path}, got {type(body).__name__}"
            )
        return body

    async def health(self) -> dict[str, Any]:
        """GET /api/health - process liveness.

        Answers 200 even with zero strips connected, which is what makes it safe
        to use as an add-on watchdog target and a config-flow validation call.
        """
        return await self._get("/api/health")

    async def devices(self) -> dict[str, Any]:
        """GET /api/devices - every strip the listener has seen.

        Includes strips that are not connected right now, because their locks
        come from saved configuration rather than a live reading, and a picker
        that went blank exactly when you wanted to see what is locked would be
        useless.
        """
        return await self._get("/api/devices")

    async def state(self, device: str | None = None) -> dict[str, Any]:
        """GET /api/state - the full state of one strip.

        `device` is a strip device id. Omit it and the controller picks, which is
        the right answer when exactly one strip exists.
        """
        path = "/api/state"
        if device:
            path = f"/api/state?device={device}"
        return await self._get(path)

    async def probe(self, device: str | None = None) -> dict[str, Any]:
        """GET /api/probe - volts and RSSI.

        READ-ONLY: no onoff route is reachable from it, so it cannot change an
        outlet, including a protected one.

        Raises:
            MttlW01StripUnreachable: the controller answered, but this strip
                could not be reached. A normal state, reported as such.
            MttlW01CannotConnect: the controller itself is unreachable.
        """
        path = "/api/probe"
        if device:
            path = f"/api/probe?device={device}"

        # A connection failure here propagates as MttlW01CannotConnect and must
        # NOT be turned into "strip unreachable" - that would report a healthy
        # controller as a dead one and point the user at the wrong hardware.
        body = await self._get(path)

        if not body.get("ok"):
            raise MttlW01StripUnreachable(
                str(body.get("error") or "the strip did not answer")
            )
        return body

    async def controller_config(self) -> dict[str, Any]:
        """GET /api/config."""
        return await self._get("/api/config")

    async def controller_diagnostics(self) -> dict[str, Any]:
        """GET /api/diagnostics."""
        return await self._get("/api/diagnostics")

    async def set_socket(self, socket: int, on: bool, device: str | None = None) -> dict[str, Any]:
        """Switch a PHYSICAL socket, by the number printed on the strip.

        Args:
            socket: Physical socket number, 1-4. NOT a firmware channel.
            on: Desired state.
            device: Optional strip device id, for multi-strip setups.

        Raises:
            MttlW01ProtectedError: the outlet is protected and this is an OFF.
            MttlW01CannotConnect: the controller is unreachable.
        """
        if not isinstance(socket, int) or not 1 <= socket <= 4:
            # Fail loudly in the client. A channel number arriving here means a
            # caller mixed up the two numberings, and the whole point of this
            # guard is that it never reaches the wire.
            raise MttlW01InvalidResponse(
                f"socket must be a physical socket number 1-4, got {socket!r}"
            )

        path = f"/api/switch/socket/{socket}"
        if device:
            path = f"{path}?device={device}"

        try:
            async with self._session.post(
                self._url(path), json={"on": on}, timeout=self._timeout
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = {}
        except asyncio.TimeoutError as err:
            raise MttlW01CannotConnect(
                f"Timed out switching socket {socket}"
            ) from err
        except aiohttp.ClientError as err:
            raise MttlW01CannotConnect(
                f"Cannot reach the controller at {self.host}: {err}"
            ) from err

        if status == 200 and isinstance(body, dict) and body.get("ok"):
            return body

        message = ""
        if isinstance(body, dict):
            message = str(body.get("error") or "")

        if status == 409:
            detail = ""
            switch = body.get("switch") if isinstance(body, dict) else None
            if isinstance(switch, dict):
                # Diagnostics only. The physical socket number is already in the
                # controller's own message; the channel is for tracing.
                detail = (
                    f" (socket {switch.get(ATTR_SOCKET)}, "
                    f"channel {switch.get(ATTR_CHANNEL)}, "
                    f"protected={switch.get(ATTR_PROTECTED)})"
                )
            raise MttlW01ProtectedError(
                (message or f"socket {socket} is protected and will not be switched off")
                + detail
            )

        raise MttlW01ApiError(
            f"Controller refused the command for socket {socket} "
            f"(HTTP {status}): {message or 'no reason given'}"
        )