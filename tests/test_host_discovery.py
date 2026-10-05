#!/usr/bin/env python3
"""Test host_discovery without a Home Assistant install.

The logic being checked is the part that cannot be verified by reading: which
add-on slug is matched, and what hostname comes out of it. Getting that wrong is
silent - the flow simply never connects - so it is worth pinning down.

The Supervisor slug of an add-on repository is a hash of the repository URL, so
it differs between the original repo, a fork, and a mirror. Each of those is a
case here, because the whole point of this module is to not care which one the
user installed.

Only the stdlib and this one module are needed. `homeassistant` is not installed
on a dev machine, so the type-only import is stubbed out and the one call into
Core (hostname_from_addon_slug) is exercised with a faithful local copy, whose
behaviour was read from the Core source.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Load host_discovery.py directly rather than importing the package.
#
# Importing moshtarak_wifi would pull in __init__.py, which imports
# homeassistant.config_entries and a good deal else - none of which is installed
# on a dev machine, and none of which has anything to do with the slug matching
# under test. Importing the single file by path keeps this test to the stdlib.

import importlib.util  # noqa: E402

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components/moshtarak_wifi/host_discovery.py"
)

# The one symbol host_discovery needs from Core is used in a type annotation,
# and `from __future__ import annotations` makes annotations lazy - but the
# import statement itself still executes, so the stub has to exist.
if "homeassistant" not in sys.modules:
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object  # type: ignore[attr-defined]
    ha.core = core  # type: ignore[attr-defined]
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core

_spec = importlib.util.spec_from_file_location("_moshtarak_host_discovery", _MODULE_PATH)
assert _spec and _spec.loader
hd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hd)

passed = 0
failed: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed.append(f"{label}: {detail}")
        print(f"  FAIL {label}  -- {detail}")


# Single source of truth for the fake Supervisor's state. install_hassio()
# writes it; get_supervisor_client() reads it. Keeping both on one holder is the
# only way the fake stays honest - an earlier version passed the add-on list to
# install_hassio() and built a *separate* empty client for the call under test,
# so every "should have matched" case silently saw an empty list.
STATE: dict = {"addons": [], "error": None, "client_raises": False}


class FakeAddon:
    def __init__(self, slug: str, url: str = "", name: str = "") -> None:
        self.slug = slug
        self.url = url
        self.name = name


class FakeAddons:
    async def list(self) -> list:
        if STATE["error"]:
            raise STATE["error"]
        return list(STATE["addons"])


class FakeClient:
    addons = FakeAddons()


def fake_hass() -> object:
    """A stand-in for hass. The fake client ignores it, as the real one does."""
    return types.SimpleNamespace()


def get_supervisor_client(hass):  # noqa: ANN001, ANN202
    if STATE["client_raises"]:
        raise STATE["client_raises"]
    return FakeClient()


def hostname_from_addon_slug(slug: str) -> str:
    return slug.replace("_", "-")


def install_hassio(addons, error=None, available=True, client_raises=None):
    """Point host_discovery at a fake hassio component, or remove it."""
    STATE["addons"] = list(addons or [])
    STATE["error"] = error
    STATE["client_raises"] = client_raises

    if not available:
        # Simulate Core-only install: the component cannot be imported, which is
        # the case host_discovery must survive.
        sys.modules.pop("homeassistant.components.hassio", None)
        sys.modules.pop("homeassistant.components", None)
        if hasattr(sys.modules["homeassistant"], "components"):
            del sys.modules["homeassistant"].components
        return

    module = types.ModuleType("homeassistant.components.hassio")
    module.get_supervisor_client = get_supervisor_client
    module.hostname_from_addon_slug = hostname_from_addon_slug
    components = types.ModuleType("homeassistant.components")
    components.hassio = module
    ha = sys.modules["homeassistant"]
    ha.components = components
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.hassio"] = module


def main() -> int:
    print("1. the real slug found on this system")
    # Measured, not assumed: the add-on installed from
    # https://github.com/Aabayoumy/moshtarak-wifi-addon has this slug.
    real = "3f742121_moshtarak_wifi"
    install_hassio([FakeAddon(real)])
    host = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(
        host == "3f742121-moshtarak-wifi",
        "resolves the installed add-on to its dash-form hostname",
        f"got {host!r}",
    )

    print("\n2. the hardcoded value that used to ship is not resolvable")
    # The point of this whole module. The old default was a0d7b954_..., which
    # was never this system's slug and would never have connected.
    check(
        hd._slug_to_hostname("a0d7b954_moshtarak_wifi") == "a0d7b954-moshtarak-wifi",
        "underscore form converts to the dash form docker accepts",
    )
    check(
        "a0d7b954" != real.split("_")[0],
        "the old default's repository prefix really is not this repository's",
        f"old=a0d7b954 real={real.split('_')[0]}",
    )

    print("\n2b. the fallback used when Core's helper is unavailable")
    # _slug_to_hostname prefers homeassistant.components.hassio's helper, and
    # only substitutes itself if that import fails. The fake hassio module
    # always supplies the helper, so without this the fallback branch is never
    # executed - a mutation that replaced `slug.replace("_", "-")` with `slug`
    # passed the suite unnoticed. That is exactly what this case is for.
    check(
        hd._slug_to_hostname("3f742121_moshtarak_wifi") == "3f742121-moshtarak-wifi",
        "Core's helper is used when it exists",
    )
    module = sys.modules.get("homeassistant.components.hassio")
    saved = module.hostname_from_addon_slug
    del module.hostname_from_addon_slug
    try:
        got = hd._slug_to_hostname("3f742121_moshtarak_wifi")
        check(
            got == "3f742121-moshtarak-wifi",
            "substitutes the dash form itself when Core's helper is gone",
            f"got {got!r}",
        )
    finally:
        module.hostname_from_addon_slug = saved

    print("\n3. forks and mirrors get different slugs, and all of them work")
    for slug in (
        "3f742121_moshtarak_wifi",
        "abcdef12_moshtarak_wifi",
        "99999999_moshtarak_wifi",
        "deadbeefcafe_moshtarak_wifi",
    ):
        install_hassio([FakeAddon(slug)])
        got = asyncio.run(hd.async_find_addon_host(fake_hass()))
        want = slug.replace("_", "-")
        check(got == want, f"{slug} -> {want}", f"got {got!r}")

    print("\n4. matching rules")
    install_hassio([FakeAddon("3f742121_moshtarak_wifi"), FakeAddon("core_ssh")])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got == "3f742121-moshtarak-wifi", "picks ours out of several add-ons", f"got {got!r}")

    # An add-on installed from the default repository would have no prefix.
    install_hassio([FakeAddon("moshtarak_wifi")])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got == "moshtarak-wifi", "handles a bare, unprefixed slug", f"got {got!r}")

    # Must not match a different add-on that merely shares a prefix or suffix
    # character. "my_moshtarak_wifi_helper" must not match.
    install_hassio([FakeAddon("my_moshtarak_wifi_helper")])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "does not match a longer unrelated slug", f"got {got!r}")

    install_hassio([FakeAddon("moshtarak_wifi_extras")])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "does not match a different add-on with a similar name", f"got {got!r}")

    print("\n5. fallback to the add-on URL when the slug does not look like ours")
    install_hassio(
        [
            FakeAddon("zzzz1111_somethingelse", url="https://github.com/Aabayoumy/moshtarak-wifi-addon"),
        ]
    )
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(
        got == "zzzz1111-somethingelse",
        "falls back to matching the add-on's declared URL",
        f"got {got!r}",
    )

    print("\n6. dict-shaped add-ons are tolerated")
    install_hassio([{"slug": "3f742121_moshtarak_wifi", "url": ""}])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got == "3f742121-moshtarak-wifi", "reads slug out of a plain dict", f"got {got!r}")

    print("\n7. absent add-on is not an error")
    install_hassio([FakeAddon("core_ssh"), FakeAddon("core_mosquitto")])
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "returns None when our add-on is not installed", f"got {got!r}")

    print("\n8. every failure mode degrades to 'ask the user', never raises")

    install_hassio([FakeAddon("3f742121_moshtarak_wifi")], error=RuntimeError("boom"))
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "survives the Supervisor being unreachable", f"got {got!r}")

    # KeyError is what Core raises when hassio has not finished setting up.
    install_hassio([FakeAddon("3f742121_moshtarak_wifi")], client_raises=KeyError("data"))
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "survives hassio not being set up yet", f"got {got!r}")

    # A Core-only install has no hassio component at all, so the import fails.
    install_hassio(None, available=False)
    got = asyncio.run(hd.async_find_addon_host(fake_hass()))
    check(got is None, "survives hassio not existing (Core-only install)", f"got {got!r}")

    install_hassio([FakeAddon("3f742121_moshtarak_wifi")])

    print("\n9. candidate list ordering and content")
    install_hassio([FakeAddon("3f742121_moshtarak_wifi")])
    hosts = asyncio.run(hd.async_candidate_hosts(fake_hass()))
    check(
        hosts == ["http://3f742121-moshtarak-wifi:8099", "http://localhost:8099"],
        "add-on host is tried first, localhost second",
        f"got {hosts!r}",
    )

    install_hassio([FakeAddon("core_ssh")])
    hosts = asyncio.run(hd.async_candidate_hosts(fake_hass()))
    check(
        hosts == ["http://localhost:8099"],
        "falls back to localhost alone when no add-on is installed",
        f"got {hosts!r}",
    )

    check(
        hd.CONTROLLER_PORT == 8099,
        "controller port is 8099, which is deliberately unpublished to the LAN",
    )

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        print("\nFAILED:")
        for f in failed:
            print(f"  - {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
