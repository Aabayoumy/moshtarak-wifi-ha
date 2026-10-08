"""Work out where the MTTL-W01 WiFi controller is listening.

Why this module exists at all
-----------------------------

The obvious thing to do is hardcode the add-on's container name:

    http://a0d7b954_tonly_mttl_w01:8099

That was the first implementation here, and it is wrong twice over.

1. The name is wrong. An add-on's slug is ``<repository slug>_<add-on slug>``.
   This add-on lives in a third-party repository, and the Supervisor derives
   the repository slug from a hash of the repository URL. So the real name is
   ``3f742121_tonly_mttl_w01``, not ``a0d7b954_...`` - the prefix was guessed
   from a different repository and would never have resolved.

2. More importantly, the name is not knowable in advance. Any user who adds the
   add-on repository from a fork, a mirror, or a Git URL with a trailing ``.git``
   gets a *different* repository slug and therefore a different container name.
   A hardcoded default is therefore guaranteed to be wrong for a real fraction of
   users, and it fails in the most confusing way available: a plausible-looking
   address in a prefilled box that simply never answers.

So the name is resolved at runtime instead, by asking the Supervisor which
add-ons are installed. That is authoritative, it adapts to a fork, and it cannot
drift from what the Supervisor actually launched.

A note on the `_` vs `-` forms
------------------------------

Both ``3f742121_tonly_mttl_w01`` and ``3f742121-tonly-mttl-w01`` exist in this
system, and only one of them resolves to the controller:

    3f742121_tonly_mttl_w01     HTTP 000   (does not resolve)
    3f742121-tonly-mttl-w01     HTTP 200   (resolves - the docker hostname)
    app_3f742121_tonly_mttl_w01 HTTP 200   (resolves - the container name)

Docker does not accept underscores in a network alias, which is why the dash
form is the one that works. Home Assistant's own
``homeassistant.components.hassio.hostname_from_addon_slug`` exists to perform
exactly this substitution; it is used here when importable, and the equivalent
one-liner is the fallback.

Everything in this module is best-effort. A Core-only install with no Supervisor,
a stripped-down container, or a future rename of the helper must all degrade to
"ask the user" rather than to an exception during setup.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# The add-on's own slug, as declared in its config.yaml. The Supervisor prefixes
# this with a repository-derived slug, so the installed slug always *ends* with
# this, never equals it (unless the add-on is someday shipped in the default
# repository, in which case equality also holds).
ADDON_SLUG: Final = "tonly_mttl_w01"

# The port the controller's HTTP API listens on. Deliberately NOT published to
# the LAN - the controller API is unauthenticated - so this address is only
# reachable from Home Assistant itself, over the add-on's container network.
CONTROLLER_PORT: Final = 8099

# Last resort for a controller running on the Home Assistant host itself, which
# is the normal setup when someone runs the controller directly rather than as
# an add-on (a container, a systemd unit, or `python3 server.py`).
FALLBACK_HOSTS: Final = (f"http://localhost:{CONTROLLER_PORT}",)


def _slug_to_hostname(slug: str) -> str:
    """Return the docker hostname for an add-on slug.

    Prefers Home Assistant's own helper so this stays aligned with how Core
    names add-on hosts; falls back to the same substitution if that helper is
    ever moved or removed.
    """
    try:
        from homeassistant.components.hassio import hostname_from_addon_slug
    except ImportError:
        return slug.replace("_", "-")
    return hostname_from_addon_slug(slug)


def _addon_slug(addon: object) -> str:
    """Return an add-on's slug, tolerating dicts as well as model objects."""
    slug = getattr(addon, "slug", None)
    if slug is None and isinstance(addon, dict):
        slug = addon.get("slug")
    return str(slug or "")


async def async_find_addon_host(hass: HomeAssistant) -> str | None:
    """Return the hostname of the installed MTTL-W01 WiFi add-on, if any.

    Returns None - never raises - when the Supervisor is unavailable, the
    add-on is not installed, or the add-on listing cannot be read. Every one of
    those is a legitimate state for a config flow to land in.
    """
    try:
        from homeassistant.components.hassio import get_supervisor_client
    except ImportError:
        # No hassio integration: a Core-only install. Not an error.
        _LOGGER.debug("hassio is unavailable; cannot look up the add-on")
        return None

    try:
        # Raises KeyError when the hassio integration has not finished setting
        # up, which is normal during early start-up.
        client = get_supervisor_client(hass)
    except (KeyError, AttributeError):
        _LOGGER.debug("Supervisor client not ready; cannot look up the add-on")
        return None

    try:
        addons = await client.addons.list()
    except Exception:  # noqa: BLE001 - any Supervisor failure means "ask the user"
        _LOGGER.debug("Could not list add-ons from the Supervisor", exc_info=True)
        return None

    # Match on the add-on slug first: it is declared in config.yaml and so is
    # stable, whereas the display name can be localised by the add-on.
    for addon in addons:
        slug = _addon_slug(addon)
        if slug == ADDON_SLUG or slug.endswith(f"_{ADDON_SLUG}"):
            return _slug_to_hostname(slug)

    # Fall back to the add-on's URL, which is also declared in config.yaml and
    # names the repository uniquely.
    for addon in addons:
        url = getattr(addon, "url", None)
        if isinstance(addon, dict):
            url = addon.get("url")
        if url and "tonly-mttl-w01-addon" in str(url):
            slug = _addon_slug(addon)
            if slug:
                return _slug_to_hostname(slug)

    return None


async def async_candidate_hosts(hass: HomeAssistant) -> list[str]:
    """Return controller URLs worth trying, most likely first.

    The first entry is the discovered add-on hostname when there is one. The
    rest are the fallback for a controller running directly on the Home
    Assistant host.
    """
    hosts: list[str] = []

    if discovered := await async_find_addon_host(hass):
        hosts.append(f"http://{discovered}:{CONTROLLER_PORT}")

    hosts.extend(h for h in FALLBACK_HOSTS if h not in hosts)
    return hosts
