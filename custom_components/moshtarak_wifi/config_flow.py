"""Config flow for Moshtarak WiFi.

Setup asks one question - where is the controller - and validates it by calling
`/api/health`, which answers 200 even with zero strips connected. That matters:
without hardware yet, a validation that required a live strip would make the
integration impossible to install and impossible to develop against, which is
the opposite of what is wanted.

The host that answered is stored, so the first successful detection is not
repeated on every reload.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MoshtarakApi, MoshtarakApiError
from .const import CONF_HOST, CONF_SCAN_INTERVAL, DEFAULT_HOSTS, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _schema(default_host: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host): str,
            vol.Optional(CONF_SCAN_INTERVAL, default=5): vol.All(
                vol.Coerce(int), vol.Range(min=5, max=300)
            ),
        }
    )


class MoshtarakConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Moshtarak WiFi."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the controller address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip().rstrip("/")
            session = async_get_clientsession(self.hass)
            api = MoshtarakApi(session, host)
            try:
                health = await api.health()
            except MoshtarakApiError as err:
                _LOGGER.debug("Controller probe failed for %s: %s", host, err)
                errors["base"] = "cannot_connect"
            else:
                if not health.get("ok"):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=host,
                        data={CONF_HOST: host},
                        options={CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 5)},
                    )

        suggested = str(user_input.get(CONF_HOST, "")) if user_input else ""
        if not suggested:
            suggested = DEFAULT_HOSTS[0]

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(suggested),
            errors=errors,
            description_placeholders={"default_host": DEFAULT_HOSTS[0]},
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle YAML import (if the user configures it that way)."""
        return await self.async_step_user(import_data)

    @staticmethod
    def async_get_options_flow(config_entry) -> Any:
        """Options flow so the poll interval can be changed without re-adding."""
        return MoshtarakOptionsFlow()


class MoshtarakOptionsFlow(OptionsFlow):
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