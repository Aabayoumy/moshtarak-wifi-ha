"""Constants for the MTTL-W01 WiFi integration.

Terminology is not cosmetic in this project, so it is fixed here once.

The strip's firmware numbers its channels its own way:

    physical socket N -> firmware channel N (identity [1, 2, 3, 4])

That mapping was re-verified 2026-10-09 by LED blink rounds on D8AA59D270AA
(an earlier rotated [2,3,4,1] reading was channel/socket confusion). Two serious incidents in this project's
history came from treating the two numberings as interchangeable, so this
integration never lets a firmware channel escape into a user-facing name, an
entity id or a unique_id. It appears in diagnostics only.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tonly_mttl_w01"

# There is deliberately no hardcoded controller host here.
#
# The first version of this integration carried
#     "http://a0d7b954_tonly_mttl_w01:8099"
# as its default, which was wrong twice over: the repository prefix was guessed
# and never resolved, and in any case an add-on's slug is derived from a hash of
# the repository URL, so every fork and mirror gets a different container name.
# See host_discovery.py, which asks the Supervisor at runtime instead.

# The controller caches reads for TONLY_MTTL_W01_POLL seconds (2 in the
# add-on's shipped options), so polling faster than this gains nothing but
# load. 5s also keeps a switch reading close to the controller's own relay
# echo time of 1-2s.
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=5)
MIN_SCAN_INTERVAL: Final = timedelta(seconds=5)
MAX_SCAN_INTERVAL: Final = timedelta(seconds=300)

# /api/probe sends an ACTIVE query to the strip. It is not a cached status read,
# so it runs on a much slower cadence than the state poll.
PROBE_INTERVAL: Final = timedelta(seconds=60)

# The device id the controller's in-process simulator answers as. Every
# simulator-backed answer carries simulated=true; this id is the second check.
SIMULATED_DEVICE_ID: Final = "SIM"

# Number of physical sockets on an MTTL-W01.
SOCKET_COUNT: Final = 4

# The controller's default outlet names are the literal English strings
# "Socket 1".."Socket 4". They are treated as ABSENT so a translation never has
# an English fragment spliced into the middle of another language. Only a name
# the user actually chose is shown.
DEFAULT_SOCKET_NAME: Final = "Socket"

ATTR_SOCKET: Final = "socket"
ATTR_CHANNEL: Final = "channel"
ATTR_PROTECTED: Final = "protected"
ATTR_DRAWS_CURRENT: Final = "draws_current"
ATTR_SIMULATED: Final = "simulated"
ATTR_REACHABLE: Final = "reachable"

CONF_HOST: Final = "host"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# Repairs
ISSUE_NO_REAL_STRIP: Final = "no_real_strip"
ISSUE_PROTECTION_UNKNOWN: Final = "protection_unknown"
ISSUE_SOCKET_ORDER_MISMATCH: Final = "socket_order_mismatch"