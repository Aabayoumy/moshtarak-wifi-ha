# Tests

Two kinds, for two different things.

## `test_host_discovery.py` — no Home Assistant needed

```
python3 tests/test_host_discovery.py
```

Pure stdlib. Checks which add-on slug is matched and what hostname comes out
of it, including the cases that cannot be got right by reading: forks and
mirrors with different repository prefixes, near-miss slugs that must *not*
match, dict-shaped add-ons, and every failure mode of the Supervisor lookup.

## `verify_live.py` — needs a running Home Assistant

```
python3 tests/verify_live.py
```

Verifies the **installed add-on** end to end, with no MTTL-W01 attached.

```
this script ──REST──> Home Assistant ──containers──> the add-on
    ^                                                          |
    |                                                          v
    └───────────── wire bytes ───────── fake strip <──TCP 10086─┘
```

Every other suite runs the controller directly and talks to it over HTTP. That
leaves one link unproved: the Supervisor launching it, publishing port 10086
but *not* 8099, and resolving a container hostname nobody can predict. This
script covers that link, so the socket/channel claims are read off bytes a
strip actually received rather than inferred from a returned state.

It needs the fake strip from the
[add-on repository](https://github.com/Aabayoumy/moshtarak-wifi-addon), which
it locates automatically, and a long-lived HA token at
`~/.config/opencode/ha-token`.

It restores the add-on's own options on the way out, including protection, so a
run that fails half way does not leave a locked outlet behind.

### What it deliberately does not do

It never asserts a power, energy or current *value*. Those readings are only as
good as the fixture's synthetic fields, and one of them is known to be written
in hex where the controller parses decimal. Asserting them would validate the
fixture, not the integration. They are checked for flowing, and nothing more.
