"""Config flow for MTTL-W01 WiFi.

Setup asks one question - where is the controller - and validates it by calling
`/api/health`, which answers 200 even with zero strips connected. That matters:
without hardware yet, a validation that required a live strip would make the
integration impossible to install and impossible to develop against, which is
the opposite of what is wanted.

Before showing the form, the flow asks the Supervisor where the add-on actually
is and probes the candidates itself. In the ordinary case - add-on installed,
running, nothing else to do - that means setup completes on a single click with
no address typed at all, and no opportunity to mistype one. Only when nothing
answers does the form appear.

The address that answered is stored, so discovery is not repeated on reload.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MttlW01Api, MttlW01ApiError
from .const import CONF_HOST, CONF_SCAN_INTERVAL, DOMAIN
from .host_discovery import async_candidate_hosts

_LOGGER = logging.getLogger(__name__)


async def _probe(session, host: str) -> bool:
    """Return True if a healthy controller answers at `host`."""
    try:
        health = await MttlW01Api(session, host).health()
    except MttlW01ApiError as err:
        _LOGGER.debug("Controller probe failed for %s: %s", host, err)
        return False
    return bool(health.get("ok"))


def _schema(default_host: str = "", scan_interval: int = 5) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Optional(
                CONF_SCAN_INTERVAL, default=scan_interval
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
        }
    )


class MttlW01ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MTTL-W01 WiFi."""

    VERSION = 1

    def __init__(self) -> None:
        """Note candidates probed before the form was ever shown."""
        self._suggested: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find the controller, or ask where it is."""
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip().rstrip("/")
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
            if await _probe(async_get_clientsession(self.hass), host):
                return self.async_create_entry(
                    title=host,
                    data={CONF_HOST: host},
                    options={
                        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 5)
                    },
                )
            # Keep whatever the user typed so a typo can be corrected rather
            # than retyped from memory.
            return self.async_show_form(
                step_id="user",
                data_schema=_schema(host, user_input.get(CONF_SCAN_INTERVAL, 5)),
                errors={"base": "cannot_connect"},
                description_placeholders={
                    "default_host": self._suggested,
                    "discovered": "yes" if self._suggested else "no",
                },
            )

        # No input yet: ask the Supervisor where the add-on is and try it. This
        # is what lets the common case finish without the user typing anything.
        session = async_get_clientsession(self.hass)
        candidates = await async_candidate_hosts(self.hass)

        for host in candidates:
            if await _probe(session, host):
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                self._suggested = host
                return self.async_create_entry(
                    title=host,
                    data={CONF_HOST: host},
                    options={CONF_SCAN_INTERVAL: 5},
                )

        # Nothing answered. Offer the best guess prefilled rather than an empty
        # box, but make clear in the field description that it is a guess.
        self._suggested = candidates[0] if candidates else ""

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self._suggested),
            errors={},
            description_placeholders={
                "default_host": self._suggested,
                "discovered": "yes" if self._suggested else "no",
            },
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle YAML import (if the user configures it that way)."""
        return await self.async_step_user(import_data)

    @staticmethod
    def async_get_options_flow(config_entry) -> Any:
        """Options flow so the poll interval can be changed without re-adding."""
        return MttlW01OptionsFlow()


class MttlW01OptionsFlow(OptionsFlow):
    """Change the poll interval without removing and re-adding the integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, 5)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=5, max=300)
                    ),
                }
            ),
        )