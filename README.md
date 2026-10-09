# MTTL-W01 WiFi — Home Assistant integration

Controls a **TONLY / LG-U+ MTTL-W01** four-socket Wi-Fi power strip in Home
Assistant.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/release/Aabayoumy/tonly-mttl-w01-ha.svg)](https://github.com/Aabayoumy/tonly-mttl-w01-ha/releases)

---

## This is only half of it

This integration **cannot talk to the strip**. It talks to a *controller*, which
is the thing that talks to the strip.

```
MTTL-W01 strip  ──dials out to TCP 10086──▶  app/add-on (the controller)
                                                  │
                                                  │ HTTP, internal only
                                                  ▼
                                           this integration
```

That split is not a preference, it is forced by the hardware. The strip never
accepts an inbound connection — ports 10086, 30888, 80 and 8080 are all closed
on it, and the address it "reports" for itself is the *source port it dialled out
from*. Something has to be listening, and putting a socket protocol client inside
Home Assistant's event loop would mean giving up the timers and history that keep
the controller honest.

So you need both:

| Part | Where | What it is |
|---|---|---|
| `tonly-mttl-w01-addon` | Home Assistant app/add-on | The controller. Speaks the vendor protocol. |
| `tonly-mttl-w01-ha` | **this repo**, via HACS | The integration. Creates the entities. |

Neither works alone.

## Install

1. **Settings → Apps → ⋮ → Repositories**, add:

   ```
   https://github.com/Aabayoumy/tonly-mttl-w01-addon
   ```

   Then install and start **MTTL-W01 WiFi** from it.

   The add-on has to be added this way once, by hand: HACS distributes
   *integrations*, and the Supervisor distributes add-ons, so the two can never
   come from a single tap.

2. In HACS → Integrations → **MTTL-W01 WiFi (TONLY MTTL-W01)** → Download.
3. Restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → MTTL-W01 WiFi.**

In the ordinary case there is nothing to type on step 4. The flow asks the
Supervisor where the add-on is, connects to it, and creates the entry on the
first click — see [Where the controller is](#where-the-controller-is). If you
installed the add-on from a fork, or run the controller some other way, the form
appears instead and you enter the address yourself.

The connection is validated with `/api/health`, which answers 200 even with
**zero strips connected**. So you can install and configure everything before the
hardware exists.

### Where the controller is

The add-on's container name is derived from a hash of its repository URL, so it
is different for a fork or a mirror and cannot be hardcoded — an earlier version
of this integration shipped a guessed name that could never have connected. The
config flow therefore asks the Supervisor which add-on is installed and derives
the address from that.

Two details worth knowing if you set it manually:

- The address is the add-on's container hostname (underscores replaced with
  dashes), **not** your Home Assistant's own address, because the controller's
  HTTP API is deliberately not published to your LAN.
- It is reachable only from Home Assistant, since the add-on runs on the
  Supervisor's internal network.

## Entities

Per strip (four sockets each):

| Entity | Notes |
|---|---|
| `switch.*_socket_1..4` | `device_class: outlet`. Keyed on **physical socket number**. |
| `sensor.*_socket_N_temperature` | Real. Field 11 of the status block. |
| `sensor.*_socket_N_power_raw` | The vendor's integer, untouched. The only part of the power reading that is not a guess. |
| `sensor.*_socket_N_power_(vendor_scale,_unverified)` | Unit W, **no `device_class`** — see below. |
| `sensor.*_socket_N_energy_(vendor_scale,_unverified)` | Unit kWh, **no `device_class`**. |
| `binary_sensor.*_socket_N_draws_power` | Something is drawing through this outlet. |
| `binary_sensor.*_socket_N_protected` | This outlet refuses to be switched off. |

Per strip:

| Entity | Notes |
|---|---|
| `sensor.*_mains_voltage` | Real, from an active query. Carries `voltage_spread_v` so disagreement between channels is visible rather than averaged away. |
| `sensor.*_wi_fi_signal_strength` | RSSI. |
| `binary_sensor.*_real_strip_connected` | **Read this one first.** |

There is a [diagnostics download](https://www.home-assistant.io/integrating/\
diagnostics/) per entry that includes the socket/channel mapping, raw per-strip
state and the controller's own view.

### After you tap a switch

The tapped switch shows the tapped value at once, from the moment the tap
starts - waiting for the echo would let a poll landing mid-flight revert
the tap in the UI for a cycle. If the command fails, or the echo never
confirms, strip truth wins again. The other three sockets hold their
last-good state for the ~12 s settling window no matter what the polls
say: polls landing inside one controller cache generation are usually the
same cached transient read twice, so agreement proves nothing and there is
no early release. A poll body that is not even well-shaped (no `switches`,
or anything but exactly sockets 1-4) is treated as no information: the
last frame is reused briefly, and only persistent rot reads as
unavailable. Every tap also resets the poll heartbeat - the re-read it
schedules pushes the next regular poll a full interval out - so the strip
always gets quiet time to settle. The policy is pure data in
`state_merge.py`, covered by `tests/test_state_merge.py`.

## Three things this integration refuses to do

### 1. Pretend the simulator is your hardware

With no strip connected, the controller still answers `/api/state` with four
healthy sockets, `reachable: true` and `settled: true` — because its built-in
simulator reports as device `SIM`. **Neither flag means your strip is answering.**

So every availability decision here asks whether a *real* strip answered.
`binary_sensor.*_real_strip_connected` is `off` when nothing real is present, the
switches go `unavailable` rather than reporting simulator state as fact, and a
**repairs issue** appears explaining why. An integration that showed four
working-looking switches in that state would be showing a facade.

### 2. Calibrate watts it cannot calibrate

The vendor divides by 1000. A measured 60 W load read back as **16.75 W**. So
the watt sensor has a unit and a state class but deliberately **no
`device_class: power`** — that class is what feeds the energy dashboard as
though a value were calibrated. Its name says *unverified* out loud, and the raw
integer sits beside it.

**There is no current sensor.** The strip exposes none: every firmware channel
returns mains voltage and nothing else. An upstream project claims
"under 50000 means milliamps"; that is false on real hardware. With no current,
watts can never be converted to amps, so a permanently `unavailable` sensor would
be noise pretending to be a feature. Voltage carries a
`current_available: false` attribute so the absence is a stated fact rather than
a mystery.

### 3. Invent meanings for fields it does not understand

Status fields 3 and 4 read `on` on **every** channel, including a completely
empty socket. They are not overload and not overheat. An earlier version of this
project's code interpreted them as safety flags and produced `overload: true` on
an empty outlet, which is how the mistake was caught — and the state-code
sensor and flag attributes that carried them are now removed outright rather
than shown raw.

## The socket/channel trap

The strip's firmware numbers its channels differently from how the sockets are
physically arranged:

```
physical socket 1 → firmware channel 2
physical socket 2 → firmware channel 3
physical socket 3 → firmware channel 4
physical socket 4 → firmware channel 1
```

This is **measured, not derived.** Getting it backwards switches the wrong
outlet, and it caused the two most serious incidents in this project's history —
once in an Android app, and once in an earlier version of this integration where
four entities were labelled with socket numbers while driving different channels
(three of four wrong; the server survived only because protection refused the
command).

So: entities are keyed on `socket`, writes go to
`POST /api/switch/socket/<n>`, and the firmware channel appears **only** in
diagnostics. The controller still exposes the channel route, labelled
`legacy, used by HA` in its own source — do not use it. `MttlW01Api` has no
`set_channel` method, and `set_socket()` rejects anything outside 1–4.

## Protection

A protected outlet refuses to be switched **off**, whatever asks — the app, Home
Assistant, the web UI, an automation, a timer, a stray `curl`. Power can always
be restored, so a protected switch is still fully switchable *on*.

The refusal happens in this integration *before* anything is sent, so a tap
fails immediately with the reason rather than appearing to work. The controller
enforces it again server-side; both paths are tested.

Configure it in the add-on options, not here. Keep the legacy `protect` list
filled in alongside `protect_by_device`: when the per-strip map is missing or
unparseable the controller falls back to it on purpose, so a typo there can never
be the reason an outlet holding a server becomes switchable. This integration
raises a repairs **error** if protection names a device id the controller has
never seen.

## Deleting a device

Settings → Devices & Services → device → **Delete** works, under one rule: you
can delete a device the controller is **no longer reporting**, and nothing
else.

The rule matters because Home Assistant deletes a device's entities along with
the device. A strip the controller still lists refuses deletion — even one that
has been unplugged: removing it would take its switches out of service, and the
next reload or restart would build them all again from the strip the controller
still lists, undoing the delete. What does delete cleanly is a leftover the
controller has stopped listing: the simulator once a real strip is answering,
or a strip unplugged across an add-on restart — the controller keeps its memory
of strips in-process and lists everything that has ever said hello until it
restarts, and only then does a departed strip become deletable, together with
all of its dark entities. And when there is no successful poll to compare
against — the entry is setting up or in error — the answer is also no: nothing
is deleted on the strength of missing data.

Two quirks of that rule, both from the controller rather than this
integration: auto mode reports the simulator whenever no real strip is
connected, so a deleted simulator device can reappear after any restart
that lands while the strip is away — an add-on restart before it redials,
or a Core restart catching the strip mid-redial — delete it again once the
strip is back; and a restart of Home Assistant itself will rebuild any
device the controller still lists, which is exactly what the refusal
protects against.

The hook is `async_remove_config_entry_device` in `__init__.py`; the decision
it makes is pure data in `device_removal.py`, covered by
[`tests/test_device_removal.py`](tests/test_device_removal.py).

## Verified behaviour

Exercised live on Home Assistant 2026.9.4 and 2026.10.0 against the controller
running with a protocol-level fake strip, capturing the exact bytes the strip
received:

| Check | Result |
|---|---|
| Controller reachable from HA, `/api/health` 200 | ✅ |
| Config flow imports, validates, creates an entry | ✅ |
| 42 entities created; real strip's entities added *dynamically* on connect | ✅ |
| Simulator-only → all switches `unavailable`, `real_strip_connected` `off`, repairs issue raised | ✅ |
| **ON physical socket 1** → strip received `up:onoff:2:on` (channel 2) | ✅ |
| **ON physical socket 2** → strip received `up:onoff:3:on` (channel 3) | ✅ |
| **OFF physical socket 2 (protected)** → refused; *no* bytes sent to the strip | ✅ |
| **OFF physical socket 1** → strip received `up:onoff:2:off` | ✅ |
| Channel 3 configured as protected surfaces as **socket 2** protected | ✅ |
| Temperature, power, energy, state code flow through | ✅ |
| Unanswerable voltage query → `unavailable`, not a fabricated `0` | ✅ |
| `settled` absent during the post-restart window → treated as unavailable | ✅ |
| Controller suites (`api`, `two_strip`, `probe`, `protect`) | 169 assertions ✅ |
| Add-on wiring contract (`tests/test_addon_contract.py`) | 71 assertions ✅ |

Run everything with `sh tests/run_all.sh` in the add-on repo — 240 assertions.

### Verified again through the real add-on

The rows above were first proved against the controller run directly. They were
then re-proved with the controller running as an installed HA add-on, with the
fake strip dialling the add-on's published callback port from another host, so
the whole chain is covered rather than just the HTTP client.

That second pass used to be a sequence of ad-hoc commands, which meant the table
below could not be reproduced by anyone else. It is now a script —
[`tests/verify_live.py`](tests/verify_live.py) — run with:

```sh
python3 tests/verify_live.py
```

It restores the add-on's own options afterwards, so a failed run does not leave
a protected outlet behind:

| Check | Result |
|---|---|
| Add-on installs, builds and starts under the Supervisor | ✅ |
| Config flow completes with **no address typed** — hostname read from the Supervisor | ✅ |
| Switch **OFF socket 1** via HA → strip received `up:onoff:2:off` | ✅ |
| Switch **ON socket 2** via HA → strip received `up:onoff:3:on` | ✅ |
| Switch states converge to match the wire after the poll interval | ✅ |
| Protection set on channel 3 → switch 2 reports `protected: true` | ✅ |
| Switch **OFF socket 2 (protected)** → refused locally, `onoff:3:off` never appears on the strip | ✅ |
| Controller HTTP API (8099) **not** reachable from the LAN; callback port (10086) is | ✅ |
| `history.db` written under the mapped `/config` path, so HA backups include it | ✅ |
| `no_real_strip` repair raised with the live host and device list | ✅ |
| Host discovery incl. fork/mirror slugs, dict add-ons, absent hassio, unreachable Supervisor | 22 assertions ✅ |
| **`tests/verify_live.py`, whole installed chain** | **39 checks ✅** |

All four socket mappings are confirmed from bytes the strip received:

| Physical socket | Firmware channel |
|---|---|
| 1 | 2 |
| 2 | 3 |
| 3 | 4 |
| 4 | 1 |

The script reads the controller through Home Assistant's diagnostics API rather
than opening port 8099, because that port is closed deliberately — a test that
required it open would defeat the reason it is closed.

**Not yet verified:** behaviour on real hardware, since no MTTL-W01 is attached
yet. Provisioning, the TCP callback and the measured socket/channel mapping all
come from the source project's history rather than from this integration's own
testing.

No test asserts a power, energy or current **value**. Those readings are only as
good as the fixture's synthetic fields, and one of them is known to be written
in hex where the controller parses decimal — asserting them would validate the
fixture rather than the integration.

### Known gap

Entities for a strip are added when the controller first reports it, but are
**never removed**. If a strip is forgotten by the controller entirely — factory
reset, a different device id, a stale entry in the controller's own config — its
entities stay in Home Assistant forever as `unavailable`, and there is no code
path that deletes them. A disconnected-but-known strip is handled correctly
(this is the common case: the controller keeps the device and reports
`connected: false`), so this only bites when the controller drops the device
outright.

## Licence

MIT for this integration. It contains **no vendor protocol code** — only an HTTP
client for the controller's REST API. The protocol implementation, and the
questions about redistributing a reverse-engineered protocol, live in the
[add-on repository](https://github.com/Aabayoumy/tonly-mttl-w01-addon); read its
`NOTICE.md` before doing anything with it.

TONLY, LG-U+ and MTTL-W01 are trademarks of their respective owners. This project
is not affiliated with, endorsed by, or supported by them.