#!/usr/bin/env python3
"""End-to-end verification against the *installed* add-on, with no hardware.

Why this exists
---------------

Every other test in this project runs the controller directly and talks to it
over HTTP. That leaves one link unproved: the Supervisor actually launches the
controller, publishes port 10086 but not 8099, puts the controller on a network
only Home Assistant can reach, and resolves a container hostname nobody can
predict. An HTTP-level test is blind to all of that, and those are exactly the
things that break.

So this script exercises the whole chain instead:

    this script ──REST──> Home Assistant ──WS/containers──> the add-on
        ^                                                             |
        |                                                             v
        └────────────── wire bytes ─────────── fake strip <──TCP 10086──┘

The fake strip is the same fixture as the controller's own suites
(``tests/fake_strip.py`` in the add-on repo), used here as a *separate host*:
it dials the add-on's published callback port, exactly as a real MTTL-W01 does
after it joins the Wi-Fi. Its log is the wire, so every claim about which
firmware channel a socket drove is read off bytes that were actually sent,
rather than inferred from a returned state.

What it proves, and what it deliberately does not
------------------------------------------------

It proves the socket/channel mapping, the protection refusal, and the
real-vs-simulated distinction. It cannot prove power, current or energy
readings, because those are only as good as the fixture's synthetic fields -
and one of them is known to be formatted wrong on purpose. The values are
therefore never asserted, only checked for flowing. Claiming a calibrated
measurement here would be worse than asserting nothing.

Requirements: the add-on installed and started, the integration added, a
long-lived HA token, and this script able to reach 10086.

Usage::

    python3 tools/verify_live.py                      # uses defaults
    python3 tools/verify_live.py --addon-host HOST    # if not discoverable
    python3 tools/verify_live.py --keep-protect       # leave protection set

It restores the add-on's own options on the way out, including protection, so
a run that fails half way does not leave a locked outlet behind.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _locate_fake_strip() -> Path:
    """Find tests/fake_strip.py, wherever this checkout keeps it.

    The fixture belongs to the add-on repository, but a user who clones only
    the integration will not have it. Rather than hardcode a sibling path that
    resolves for the author and silently breaks for everyone else, look in the
    plausible places and, if it is genuinely missing, say so plainly - including
    the URL to fetch it from. A test harness that cannot explain why it is
    unusable is a harness people learn to ignore.
    """
    candidates = [
        HERE / "fake_strip.py",                                  # beside this file
        HERE.parent / "tests" / "fake_strip.py",                  # this repo's tests/
        HERE.parent / "addon" / "tests" / "fake_strip.py",       # inside the add-on repo
        HERE.parent.parent / "addon" / "tests" / "fake_strip.py",  # sibling repo, monorepo layout
        HERE.parent.parent / "tonly-mttl-w01-addon" / "tests" / "fake_strip.py",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()

    raise SystemExit(
        "Could not find fake_strip.py.\n\n"
        "It lives in the add-on repository:\n"
        "  https://github.com/Aabayoumy/tonly-mttl-w01-addon\n\n"
        "Clone it next to this repository, or pass its path explicitly.\n"
        "Looked in:\n"
        + "\n".join(f"  {p}" for p in candidates)
    )


FAKE_STRIP = _locate_fake_strip()

DEFAULT_HA = "http://10.0.0.11:8123"
DEFAULT_DEVID = "D8AA59D270AA"

# The measured mapping. Physical socket -> firmware channel. Not derived from
# docs, not derived from the protocol layout: read off the bytes the strip
# received. socket N -> ch N (identity, blink-verified 2026-10-09).
SOCKET_TO_CHANNEL = {1: 1, 2: 2, 3: 3, 4: 4}

# The add-on's declared options, restored at the end of the run.
DEFAULT_ADDON_OPTIONS = {
    "mode": "auto",
    "poll": 2,
    "protect": "",
    "protect_by_device": "",
    "history_interval": 20,
    "history_keep_h": 48,
}


class Checks:
    """Assertion counter that keeps going, so one failure does not hide others."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self._section = ""

    def section(self, title: str) -> None:
        self._section = title
        print(f"\n{title}")

    def ok(self, label: str, detail: str = "") -> bool:
        print(f"  ok   {label}")
        self.passed += 1
        return True

    def fail(self, label: str, detail: str = "") -> None:
        where = f"{self._section}: " if self._section else ""
        print(f"  FAIL {label}")
        if detail:
            print(f"       {detail}")
        self.failed.append(f"{where}{label}" + (f" -- {detail}" if detail else ""))

    def check(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            return self.ok(label)
        self.fail(label, detail)
        return False

    def report(self) -> int:
        total = self.passed + len(self.failed)
        print("-" * 60)
        print(f"TOTAL: {self.passed}/{total} passed, {len(self.failed)} failed")
        if self.failed:
            print("\nproblems:")
            for f in self.failed:
                print(f"  - {f}")
            return 1
        return 0


# --------------------------------------------------------------------------
# Home Assistant REST
# --------------------------------------------------------------------------


class HA:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, payload: dict | None = None,
             timeout: int = 25) -> tuple[int, object]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                return resp.status, (json.loads(body) if body.strip() else None)
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")
            return err.code, body

    def states(self) -> list[dict]:
        _, body = self._req("GET", "/api/states", timeout=40)
        return body if isinstance(body, list) else []

    def state(self, entity_id: str) -> dict | None:
        _, body = self._req("GET", f"/api/states/{entity_id}")
        return body if isinstance(body, dict) else None

    def call(self, domain: str, service: str, **data) -> tuple[int, object]:
        return self._req("POST", f"/api/services/{domain}/{service}", data)

    def entries(self, domain: str) -> list[dict]:
        # Config entries are only exposed over the websocket API, so this uses
        # the same stdlib client as tools/ha_ws.py rather than duplicating it.
        sys.path.insert(0, str(HERE))
        from ha_ws import call  # type: ignore

        result = call("config_entries/get")
        return [
            e for e in (result.get("result") or [])
            if e.get("domain") == domain
        ]


# --------------------------------------------------------------------------
# Supervisor, through Core's websocket proxy
# --------------------------------------------------------------------------


def sup(method: str, endpoint: str, payload: dict | None = None,
        timeout: float = 120) -> tuple[bool, object]:
    """Call a Supervisor endpoint via Core's `supervisor/api` websocket proxy.

    The REST route (/api/hassio/...) answers 401 for an admin token, and
    /addons/repositories answers 403 for the add-on manager role, so this
    proxy is the only path that works for add-on options and restarts.

    The timeout is not optional. Core's proxy defaults to 10 seconds, and an
    add-on restart takes far longer, so without raising it a perfectly
    successful restart returns `unknown_error` with an empty message. That is
    indistinguishable from a real failure, which is a fine way to waste an
    afternoon.
    """
    sys.path.insert(0, str(HERE))
    from ha_ws import supervisor  # type: ignore

    reply = supervisor(endpoint, method=method, data=payload, timeout=timeout)
    return bool(reply.get("success")), reply.get("result")


def addon_hostname(slug: str) -> str:
    """Container hostname for an add-on slug; dashes, which is what resolves."""
    return slug.replace("_", "-")


def discover_addon_slug(prefix_guess: str = "tonly_mttl_w01") -> str | None:
    """Find the installed add-on's slug, asking the Supervisor.

    GET /addons answers ``{"addons": [...]}``, not a bare list. An earlier
    version of this function checked ``isinstance(addons, list)``, found a dict,
    and returned None - so it reported "add-on is not installed" while the
    add-on was sitting there, started, one line further down the same response.
    A shape mismatch must not be allowed to masquerade as an absent thing.
    """
    ok, result = sup("get", "/addons")
    if not ok:
        return None

    if isinstance(result, dict):
        listed = result.get("addons")
    else:
        listed = result
    if not isinstance(listed, list):
        raise TypeError(
            f"/addons returned {type(result).__name__}, expected a list or "
            f"a dict containing 'addons'"
        )

    for addon in listed:
        if not isinstance(addon, dict):
            continue
        slug = str(addon.get("slug", ""))
        if slug == prefix_guess or slug.endswith(f"_{prefix_guess}"):
            return slug
    return None


# --------------------------------------------------------------------------
# The fake strip, running in this process as a separate connection
# --------------------------------------------------------------------------


class FakeStrip:
    """Runs tests/fake_strip.py as a child process and exposes its wire log.

    Using the real fixture rather than a reimplementation matters: if this
    script and the controller suites ever disagreed about the protocol, the
    tests would be validating each other instead of the controller.
    """

    def __init__(self, dial_host: str, dial_port: int, devid: str,
                 log_path: Path) -> None:
        self.devid = devid
        self.log_path = log_path
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE_STRIP), "--dial", str(dial_port),
             "--host", dial_host, "--devid", devid],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        with self.log_path.open("a") as fh:
            for line in self.proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                with self._lock:
                    self._lines.append(line)
                fh.write(line + "\n")
                fh.flush()

    def commands(self, pattern: str = "") -> list[str]:
        """Wire commands the strip received, oldest first."""
        with self._lock:
            lines = list(self._lines)
        out = []
        for line in lines:
            if "<< " not in line:
                continue
            payload = line.split("<< ", 1)[1].strip().strip("'\"")
            if payload.startswith("up:onoff:") and pattern in payload:
                out.append(payload)
        return out

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --------------------------------------------------------------------------
# Waiting
# --------------------------------------------------------------------------


def wait_for(predicate, timeout: float, interval: float = 2.0,
             what: str = "") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    if what:
        print(f"  (timed out after {timeout:.0f}s waiting for {what})")
    return False


def of_domain(entity_id: str, domain: str) -> bool:
    """True if this entity belongs to `domain`.

    Needed because several entity families share a name prefix. Every simulator
    entity whose name starts with `socket_` is not a switch: there are also
    `socket_N_protected` binary sensors and `socket_N_power_*` / `socket_N_energy_*`
    / `socket_N_temperature` sensors under that same prefix. An earlier version of
    this script filtered on the name and so swept the protected binary sensors in
    with the switches - then reported them as switches wrongly reading `off`, when
    they were correctly reporting that no outlet is protected.

    Domain is the only unambiguous discriminator, so it is the one used.
    """
    return entity_id.startswith(f"{domain}.")


def find_entities(ha: HA, devid: str) -> dict[str, str]:
    """Map this strip's entity ids by their trailing slug fragment."""
    suffix = devid.lower()
    found: dict[str, str] = {}
    for state in ha.states():
        eid = state.get("entity_id", "")
        if suffix not in eid:
            continue
        tail = eid.split(suffix + "_", 1)[-1] if suffix + "_" in eid else ""
        if tail:
            found.setdefault(tail, eid)
    return found


# --------------------------------------------------------------------------
# The verification itself
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ha", default=DEFAULT_HA)
    ap.add_argument("--addon-host", default="",
                    help="controller hostname; discovered if omitted")
    ap.add_argument("--devid", default=DEFAULT_DEVID)
    ap.add_argument("--token-file",
                    default=os.path.expanduser("~/.config/opencode/ha-token"))
    ap.add_argument("--callback-port", type=int, default=10086,
                    help="add-on's published callback port")
    ap.add_argument("--keep-protect", action="store_true",
                    help="do not clear protection at the end")
    ap.add_argument("--log", default=str(HERE / "verify_live.log"))
    args = ap.parse_args()

    ck = Checks()

    if not args.token_file or not Path(args.token_file).exists():
        print(f"no HA token at {args.token_file}")
        print("create one at: HA UI -> Profile -> Security -> "
              "Long-lived access tokens")
        return 2
    token = Path(args.token_file).read_text().strip().splitlines()[0]
    ha = HA(args.ha, token)

    print("Verifying the installed MTTL-W01 WiFi add-on, with no hardware.")
    print(f"  Home Assistant : {args.ha}")
    print(f"  fake strip     : {FAKE_STRIP}")

    # -- 1. prerequisites ------------------------------------------------
    ck.section("1. the integration is set up")

    entries = ha.entries("tonly_mttl_w01")
    if not ck.check(bool(entries), "integration has a config entry",
                    "add it via Settings -> Devices & Services"):
        return ck.report()

    entry = entries[0]
    entry_id = str(entry.get("entry_id", ""))
    host = args.addon_host or str(entry.get("title", ""))
    ck.check(entry.get("state") == "loaded",
             "config entry is loaded", f"state={entry.get('state')}")
    ck.check(bool(host), "controller host is known", host)
    print(f"  controller host: {host}")

    # -- 2. the add-on, as the Supervisor sees it ------------------------
    ck.section("2. the add-on, as the Supervisor sees it")

    # The slug is needed later to set protection, and a None would end up
    # interpolated into a URL as the literal string "None". Stop here instead.
    slug = discover_addon_slug()
    if not ck.check(bool(slug), "add-on is installed and visible to the Supervisor",
                    "add it via Settings -> Apps -> Repositories"):
        return ck.report()
    assert slug is not None  # for the type checker; guaranteed by the check above
    print(f"  slug           : {slug}")
    ck.check(host.endswith(f"{addon_hostname(slug)}:8099"),
             "config entry points at that add-on's own hostname", host)

    # Proves the Supervisor proxy works at all, which the protection steps below
    # depend on.
    ck.check(bool(sup("get", "/supervisor/info")[0]),
             "Supervisor reachable through Core's proxy")

    # The controller API must NOT be published to the LAN. This is a security
    # claim, so it is checked from outside, not asserted from the config file.
    lan_ip = args.ha.split("//")[-1].split(":")[0]
    reach = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "6",
         f"http://{lan_ip}:8099/api/health"],
        capture_output=True, text=True).stdout.strip()
    ck.check(reach == "000",
             "controller HTTP API is NOT reachable from the LAN",
             f"got HTTP {reach} from {lan_ip}:8099 - expected nothing at all")

    # -- 3. no strip yet: everything must be honest -----------------------
    ck.section("3. with no real strip, nothing pretends otherwise")

    devs = _controller_devices(ha, entry_id)
    sim_only = bool(devs) and all(d.get("simulated") for d in devs)

    if sim_only:
        ents = find_entities(ha, "simulator")
        switches = [e for e in ents.values() if of_domain(e, "switch")]
        states = {e: (ha.state(e) or {}).get("state") for e in switches}
        ck.check(all(s == "unavailable" for s in states.values()),
                 "simulator switches are unavailable, not fake-on",
                 str(states))
    else:
        print("  (a real strip is already connected; skipping the "
              "simulator-only checks)")
        sim_only = False

    # -- 4. connect a fake strip, as a separate host would ----------------
    ck.section("4. a strip connects to the add-on's published callback port")

    log_path = Path(args.log)
    if log_path.exists():
        log_path.unlink()

    # The add-on dials nothing: the strip dials the add-on. Reaching 10086
    # from here is exactly what a real strip on the Wi-Fi does.
    nc = subprocess.run(["nc", "-z", "-w", "5", lan_ip,
                         str(args.callback_port)],
                        capture_output=True, text=True)
    ck.check(nc.returncode == 0,
             f"callback port {args.callback_port} is published",
             f"nc failed: {nc.stderr.strip()}")

    strip = FakeStrip(lan_ip, args.callback_port, args.devid, log_path)
    try:
        devs = _controller_devices(ha, entry_id)
        appeared = wait_for(
            lambda: any(d.get("devid", "").upper() == args.devid.upper()
                        and not d.get("simulated")
                        for d in _controller_devices(ha, entry_id)),
            timeout=45, what="the strip to appear on the controller")
        ck.check(appeared, "controller reports the strip as a real device",
                 f"last seen: {devs}")
        if not appeared:
            return ck.report()

        real = [d for d in _controller_devices(ha, entry_id)
                if d.get("devid", "").upper() == args.devid.upper()]
        if real:
            print(f"  device         : {real[0].get('devid')} "
                  f"({real[0].get('model')}) from {real[0].get('remote')}")

        # -- 5. entities appear, and only for the real strip --------------
        ck.section("5. entities")

        ents = wait_for(lambda: len(find_entities(ha, args.devid)) > 0,
                        timeout=40, what="entities")
        ents = find_entities(ha, args.devid)
        ck.check(bool(ents), "entities exist for the real strip",
                 f"found: {sorted(ents)}")

        for want in ("socket_1", "socket_2", "socket_3", "socket_4"):
            ck.check(f"socket_{want.split('_')[1]}" in ents,
                     f"switch for {want} exists")
        for want in ("real_strip_connected",):
            ck.check(want in ents, f"binary_sensor.{want} exists")

        # Wait for the integration to actually catch up before reading anything
        # or switching anything.
        #
        # The entities appear in the registry as soon as the coordinator sees
        # the device, but they stay `unavailable` until the strip answers a
        # state query and the post-restart window clears. Reading
        # real_strip_connected immediately therefore catches `off` on a strip
        # that is present and working, and a turn_on sent at that moment is
        # accepted by the service call but never reaches the controller.
        #
        # Both were real: an early run reported "real_strip_connected is on"
        # failing with state=off and "socket 1 ON reached the strip" failing
        # with nothing on the wire, while sockets 2, 3 and 4 all passed in the
        # same loop. Only the first command was affected, which is the
        # signature of a readiness race rather than a broken outlet.
        ready = wait_for(
            lambda: ((ha.state(ents.get("real_strip_connected", "")) or {})
                     .get("state") == "on"),
            timeout=45, what="real_strip_connected to turn on")
        conn = ha.state(ents.get("real_strip_connected", "")) or {}
        ck.check(ready, "real_strip_connected is on for a real strip",
                 f"state={conn.get('state')}")

        switches_ready = wait_for(
            lambda: all(
                (ha.state(ents.get(f"socket_{n}", "")) or {}).get("state")
                not in (None, "unavailable", "unknown")
                for n in (1, 2, 3, 4)
                if f"socket_{n}" in ents
            ),
            timeout=45, what="all four switches to become available")
        ck.check(switches_ready,
                 "all four switches become available once the strip answers",
                 str({n: (ha.state(ents.get(f"socket_{n}", "")) or {}).get("state")
                      for n in (1, 2, 3, 4) if f"socket_{n}" in ents}))

        # The simulated device's switches must stay unavailable even now.
        sim_ents = find_entities(ha, "simulator")
        sim_switches = [e for e in sim_ents.values()
                        if of_domain(e, "switch")]
        if sim_switches:
            sim_states = [(ha.state(e) or {}).get("state") for e in sim_switches]
            ck.check(all(s == "unavailable" for s in sim_states),
                     "simulator switches stay unavailable alongside a real strip",
                     str(sim_states))

        # -- 6. the socket/channel mapping, read off the wire ------------
        ck.section("6. the socket/channel mapping, from bytes on the wire")

        for socket_no in (1, 2, 3, 4):
            entity = ents.get(f"socket_{socket_no}")
            if not entity:
                ck.fail(f"socket {socket_no} has a switch to test")
                continue
            want_ch = SOCKET_TO_CHANNEL[socket_no]

            before = strip.commands()
            ha.call("switch", "turn_on", entity_id=entity)
            sent = wait_for(
                lambda: len(strip.commands()) > len(before), timeout=25,
                what=f"a command after switching socket {socket_no}")
            if not sent:
                ck.fail(f"socket {socket_no} ON reached the strip",
                        "no onoff command appeared in the strip log")
                continue

            cmd = strip.commands()[-1]
            want_cmd = f"up:onoff:{want_ch}:on"
            ck.check(cmd == want_cmd,
                     f"ON physical socket {socket_no} -> {want_cmd}",
                     f"strip received {cmd!r}")

            # And the state must converge, not be optimistically flipped.
            settled = wait_for(
                lambda: (ha.state(entity) or {}).get("state") == "on",
                timeout=25, what=f"socket {socket_no} to read on")
            ck.check(settled,
                     f"socket {socket_no} reads on after the command",
                     f"state={(ha.state(entity) or {}).get('state')}")

            before = strip.commands()
            ha.call("switch", "turn_off", entity_id=entity)
            sent = wait_for(
                lambda: len(strip.commands()) > len(before), timeout=25,
                what=f"an OFF after switching socket {socket_no}")
            if sent:
                cmd = strip.commands()[-1]
                ck.check(cmd == f"up:onoff:{want_ch}:off",
                         f"OFF physical socket {socket_no} -> "
                         f"up:onoff:{want_ch}:off",
                         f"strip received {cmd!r}")

        # -- 7. protection refuses OFF, locally ---------------------------
        ck.section("7. a protected outlet refuses OFF before anything is sent")

        prot_socket = 2          # channel 3
        prot_channel = SOCKET_TO_CHANNEL[prot_socket]
        entity = ents.get(f"socket_{prot_socket}")

        _set_protection(slug, args.devid, prot_channel)
        _restart_addon(slug)
        time.sleep(12)

        devs = _controller_devices(ha, entry_id)
        locked = [d for d in devs
                  if d.get("devid", "").upper() == args.devid.upper()]
        ck.check(bool(locked) and prot_socket in (locked[0].get(
            "protected_sockets") or []),
            f"controller reports channel {prot_channel} as socket "
            f"{prot_socket} protected",
            str(locked[0].get("protected_sockets") if locked else devs))

        if entity:
            wait_for(lambda: (ha.state(entity) or {}).get("attributes", {}).get(
                "protected") is True, timeout=25,
                what="the switch to report protected")
            attrs = (ha.state(entity) or {}).get("attributes", {})
            ck.check(attrs.get("protected") is True,
                     f"switch {prot_socket} reports protected=true",
                     f"attributes={ {k: v for k, v in attrs.items() if 'protect' in k} }")

            ha.call("switch", "turn_on", entity_id=entity)
            wait_for(lambda: (ha.state(entity) or {}).get("state") == "on",
                     timeout=25, what=f"socket {prot_socket} to come on")

            # Sanity anchor, and the reason the rest of this check is trusted.
            #
            # The obvious way to write this is "did up:onoff:<ch>:off appear?"
            # which becomes "compare the count before with the count after".
            # That comparison is worthless on its own: if the strip stopped
            # logging commands altogether, or the fixture was pointed at a
            # different channel, both counts stay at zero and the guard passes
            # having verified nothing. It was proved worthless by mutation -
            # replacing the comparison with a literal True still passed the
            # whole suite.
            #
            # So liveness is established first, with an OFF on a socket that is
            # NOT protected, and shown to reach the wire. After that, "nothing
            # appeared" means the refusal happened locally rather than being
            # rejected by the strip.
            #
            # Note that no check here requires wire traffic to increase across
            # the refused attempt. A draft did, reasoning that a live link is a
            # chatty one; the run failed on it, because the strip only logs what
            # it is sent and a correct implementation sends nothing at all. The
            # refusal is "never sent", not "sent and rejected".
            #
            # So the OFF command is first shown to reach the wire on a socket
            # that is NOT protected. Only then is the protected attempt made,
            # and only then does "nothing appeared" mean something: it means
            # the fixture is demonstrably capable of logging that exact
            # command, and did not.
            unprotected = ents.get("socket_1")
            if unprotected:
                control_ch = SOCKET_TO_CHANNEL[1]
                ha.call("switch", "turn_on", entity_id=unprotected)
                time.sleep(2)
                ha.call("switch", "turn_off", entity_id=unprotected)
                got_control = wait_for(
                    lambda: f"up:onoff:{control_ch}:off" in strip.commands(),
                    timeout=25,
                    what="the control OFF command on an unprotected socket")
                ck.check(got_control,
                         "control: an UNPROTECTED socket's OFF does reach the "
                         "strip",
                         "so a missing protected OFF is meaningful, not just "
                         "a dead log")
                ha.call("switch", "turn_off", entity_id=unprotected)
                time.sleep(2)

            target_off = f"up:onoff:{prot_channel}:off"
            # Only commands that arrive from here on count. Scanning the whole
            # log would find socket 2's legitimate, unprotected OFF from the
            # mapping section above - which is exactly the command being looked
            # for - and fail on a correct implementation. That is not a subtle
            # distinction: it is the difference between "this attempt sent
            # something" and "this attempt sent nothing".
            mark = len(strip.commands())
            ha.call("switch", "turn_off", entity_id=entity)
            time.sleep(8)
            during = strip.commands()[mark:]

            ck.check(not during,
                     "turning a protected outlet off sends NO bytes",
                     f"the strip received {during} during the attempt"
                     if during else
                     f"no command of any kind appeared ({target_off} included)")

            # The narrower claim, checked separately because it is the one that
            # specifically matters: not merely "something else was sent", but
            # this exact command. Kept as its own check so that a failure of
            # the broad one above cannot be mistaken for proof that the OFF
            # itself was the thing that went out.
            ck.check(target_off not in during,
                     f"the protected OFF command {target_off} specifically "
                     f"was not sent",
                     str(during))

            still = (ha.state(entity) or {}).get("state")
            ck.check(still == "on",
                     "the protected outlet is still on afterwards",
                     f"state={still}")

        # -- 8. readings flow, but are never asserted ---------------------
        ck.section("8. readings flow through; their values are not claimed")

        sensors = [e for e in ents.values()
                   if of_domain(e, "sensor")
                   and (e.endswith("power_unverified")
                        or e.endswith("energy_vendor_scale_unverified")
                        or e.endswith("temperature"))]
        ck.check(bool(sensors), "power/energy/temperature sensors exist",
                 f"found: {sorted(sensors)}")

        voltages = [e for k, e in ents.items() if k.endswith("mains_voltage")]
        for e in voltages:
            st = ha.state(e) or {}
            ck.check(st.get("state") not in (None, "unknown"),
                     f"{e.split('.')[-1]} reports a state",
                     f"state={st.get('state')}")

        # A watts reading is never given device_class: power, and its name says
        # so. If that ever changes, the claim in the README is wrong.
        for e in sensors:
            st = ha.state(e) or {}
            dc = (st.get("attributes") or {}).get("device_class")
            if dc in ("power", "energy"):
                ck.fail(f"{e.split('.')[-1]} must not claim device_class={dc}",
                        "an uncalibrated reading cannot be presented as a "
                        "measured power or energy value")

        print("  note: values are not asserted. The fixture's synthetic fields "
              "are\n        not calibrations, and one of them is known to be "
              "hex-formatted.")

        # -- 9. restore ---------------------------------------------------
        if not args.keep_protect:
            print("\n  restoring the add-on's options")
            _reset_options(slug)
            _restart_addon(slug)
            time.sleep(8)
            devs = _controller_devices(ha, entry_id)
            ck.check(all(not (d.get("protected_sockets") or [])
                         for d in devs),
                     "protection cleared on the way out")

    finally:
        strip.stop()

    print(f"\nwire log: {log_path}")
    return ck.report()


# --------------------------------------------------------------------------
# Reading the controller, and changing the add-on's configuration
# --------------------------------------------------------------------------


def _diagnostics(ha: HA, entry_id: str) -> dict:
    """The integration's own view of the controller, via HA's diagnostics API.

    This is how a controller that is deliberately unreachable from the LAN is
    inspected from a machine that is not Home Assistant.

    The alternative - exposing 8099, or shelling over SSH into the container -
    would each defeat the reason the port is unpublished in the first place, or
    make the verification depend on SSH access the project does not promise.
    Diagnostics already carries the controller's device list, its protection
    map, and the socket/channel map, which is all this script needs.

    It is also the most honest view available: it is what the integration
    actually decided to create entities from, so a disagreement between the
    controller and the entities shows up here rather than being smoothed over.
    """
    _, body = ha._req("GET", f"/api/diagnostics/config_entry/{entry_id}",
                      timeout=60)
    if not isinstance(body, dict):
        return {}
    return body.get("data", {})


def _controller_devices(ha: HA, entry_id: str) -> list[dict]:
    """The controller's device list, as Home Assistant sees it."""
    diag = _diagnostics(ha, entry_id)
    devices = diag.get("controller", {}).get("devices", {})
    listed = devices.get("devices")
    return listed if isinstance(listed, list) else []


def _set_protection(slug: str, devid: str, channel: int) -> bool:
    """Protect one firmware channel on one strip, via the add-on's options.

    Setting protection through the add-on's declared configuration rather than
    by poking the controller directly is deliberate: it is the same route a real
    user takes, so this also proves the option is honoured end to end. If the
    option were ignored, the protection check below would fail - which is the
    behaviour worth catching.
    """
    options = dict(DEFAULT_ADDON_OPTIONS)
    options["protect_by_device"] = f"{devid}={channel}"
    ok, _ = sup("post", f"/addons/{slug}/options", {"options": options})
    return ok


def _reset_options(slug: str) -> bool:
    ok, _ = sup("post", f"/addons/{slug}/options",
                {"options": dict(DEFAULT_ADDON_OPTIONS)})
    return ok


def _restart_addon(slug: str) -> bool:
    # A restart re-reads the options, and the controller caches its
    # configuration at boot.
    ok, _ = sup("post", f"/addons/{slug}/restart", {}, timeout=180)
    return ok


if __name__ == "__main__":
    raise SystemExit(main())