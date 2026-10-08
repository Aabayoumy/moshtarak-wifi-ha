#!/usr/bin/env python3
"""Minimal Home Assistant WebSocket client, standard library only.

The HA frontend does not call Supervisor endpoints over REST. It sends a
WebSocket command of the form `supervisor/api/<path>` to the backend, which
proxies it to the Supervisor using its own admin token. The REST equivalent
(/api/hassio/...) rejects a user's long-lived token, so the WebSocket route is
the one to try.

This exists because no websocket library is installed on this machine and
curl has no ws:// support, and because pulling in a dependency to send four
short JSON frames is not a good trade.

Usage:
    python3 ha_ws.py supervisor/api/addons/repositories post '{"repository":"..."}'
    python3 ha_ws.py get_states            # a built-in sanity check

Reading the token: HA_TOKEN, HOME_ASSISTANT_TOKEN, or ~/.config/opencode/ha-token.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
from pathlib import Path

# Overridable so the suite can be pointed at a tunnel (HA_HOST=127.0.0.1
# HA_PORT=18123). On some hosts the OS denies *this* process direct LAN
# access while curl/ssh are allowed; an ssh -L forward sidesteps that.
HA_HOST = os.environ.get("HA_HOST", "10.0.0.11")
HA_PORT = int(os.environ.get("HA_PORT", "8123"))
TOKEN_FILES = (
    Path.home() / ".config/opencode/ha-token",
    Path.home() / ".hass-token",
)


def get_token() -> str:
    for var in ("HA_TOKEN", "HOME_ASSISTANT_TOKEN", "HASS_TOKEN"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    for path in TOKEN_FILES:
        if path.is_file():
            value = path.read_text().strip().splitlines()
            if value and value[0].strip():
                return value[0].strip()
    raise SystemExit(
        "no HA token found; set HA_TOKEN or write one to ~/.config/opencode/ha-token"
    )


class WS:
    """Just enough WebSocket to send JSON and read JSON. Text frames only."""

    def __init__(self, host: str, port: int, path: str, timeout: int = 30) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()

        # RFC 6455 handshake. The Host header and version 13 are mandatory;
        # the rest just tells the server we do text frames.
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("connection closed during handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n")[0]:
            raise SystemExit(
                "handshake refused:\n" + header.decode(errors="replace")[:400]
            )
        self.buf = rest

    def _recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        # Client frames MUST be masked.
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        frame = bytearray([0x81])  # FIN + text
        if n < 126:
            frame.append(0x80 | n)
        elif n < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", n)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", n)
        frame += mask + masked
        self.sock.sendall(bytes(frame))

    def recv(self) -> dict:
        while True:
            b0, b1 = self._recv(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv(8))[0]
            mask = self._recv(4) if masked else None
            data = self._recv(length)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                raise SystemExit("server closed the connection")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                return json.loads(data.decode())

    def close(self) -> None:
        try:
            self.sock.sendall(b"\x88\x80" + os.urandom(4))
        except OSError:
            pass
        self.sock.close()


def supervisor(endpoint: str, method: str = "get", data: dict | None = None,
                params: dict | None = None, timeout: float | None = None) -> dict:
    """Call a Supervisor endpoint through Home Assistant's own proxy.

    The frontend never talks to the Supervisor directly. It sends a
    `supervisor/api` WebSocket command carrying {endpoint, method, data, params},
    and Core forwards it using its own admin token. That proxy refuses REST
    requests authenticated with a user's long-lived token, but the WebSocket
    route works for an admin user's token, which is what this uses.

    `endpoint` is the Supervisor path, e.g. "/addons/repositories".

    `timeout` matters: Core's proxy defaults to 10 seconds, and a docker build
    takes half a minute. Without raising it a perfectly successful install comes
    back as `unknown_error` with an empty message, because the *reply* was lost
    rather than the operation. That reads exactly like a real failure, so it is
    worth knowing the difference before going hunting through build logs.
    """
    msg: dict = {
        "id": 1,
        "type": "supervisor/api",
        "endpoint": endpoint,
        "method": method,
    }
    if data is not None:
        msg["data"] = data
    if params is not None:
        msg["params"] = params
    if timeout is not None:
        msg["timeout"] = timeout

    ws = WS(HA_HOST, HA_PORT, "/api/websocket")
    try:
        first = ws.recv()
        if first.get("type") != "auth_required":
            print(f"unexpected greeting: {first}")
        ws.send({"type": "auth", "access_token": get_token()})
        authed = ws.recv()
        # The server signals success by type, not by a boolean field.
        if authed.get("type") != "auth_ok":
            raise SystemExit(f"auth failed: {authed}")

        ws.send(msg)
        while True:
            reply = ws.recv()
            if reply.get("type") == "event" and reply.get("id") != 1:
                continue  # unrelated subscription traffic
            if reply.get("id") == 1:
                return reply
    finally:
        ws.close()


def call(command: str, method: str = "get", data: dict | None = None,
         wait: bool = True) -> dict:
    ws = WS(HA_HOST, HA_PORT, "/api/websocket")
    try:
        first = ws.recv()
        if first.get("type") != "auth_required":
            print(f"unexpected greeting: {first}")
        ws.send({"type": "auth", "access_token": get_token()})
        authed = ws.recv()
        # The server signals success by type, not by a boolean field.
        if authed.get("type") != "auth_ok":
            raise SystemExit(f"auth failed: {authed}")

        msg: dict = {"id": 1, "type": command}
        if method and method != "get":
            msg["method"] = method
        if data is not None:
            msg["data"] = data
        ws.send(msg)

        if not wait:
            return {"sent": msg}

        while True:
            reply = ws.recv()
            if reply.get("type") == "event" and reply.get("id") != 1:
                continue  # unrelated subscription traffic
            if reply.get("id") == 1:
                return reply
    finally:
        ws.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    if sys.argv[1] == "sup":
        # supervisor/api proxy:  python3 ha_ws.py sup /addons/repositories post '{...}'
        endpoint = sys.argv[2]
        method = sys.argv[3] if len(sys.argv) > 3 else "get"
        payload = json.loads(sys.argv[4]) if len(sys.argv) > 4 else None
        # Anything that builds or installs can outrun Core's 10s proxy default,
        # so allow an explicit override:  sup /addons/<slug>/install post '{}' 900
        timeout = float(sys.argv[5]) if len(sys.argv) > 5 else 30
        out = supervisor(endpoint, method, payload, timeout=timeout)
        # No truncation here. A silently clipped response is worse than a long
        # one: it still looks like valid JSON to the eye, but json.load chokes
        # on it, and /addons is exactly the endpoint big enough to be clipped.
        print(json.dumps(out, indent=2))
        sys.exit(0 if out.get("success") else 1)

    if sys.argv[1] == "get_states":
        # Proves the whole path works before anything risky is attempted.
        out = call("get_states")
        states = out.get("result") or []
        print(f"auth ok, {len(states)} entities visible")
        mw = [
            s for s in states
            if "mttl" in s.get("entity_id", "") or "tonly_mttl_w01" in s.get("entity_id", "")
        ]
        print(f"{len(mw)} tonly/mttl entities")
        for s in sorted(mw, key=lambda x: x["entity_id"]):
            print(f"  {s['state']:<12} {s['entity_id']}")
    else:
        command = sys.argv[1]
        method = sys.argv[2] if len(sys.argv) > 2 else "get"
        payload = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
        # Never truncate a JSON response. A clipped one still looks like JSON
        # to the eye but fails to parse, and config_entries/get is easily big
        # enough to hit a character cap - which produced a confusing
        # "Unfinished string at EOF" from jq rather than an obvious symptom.
        print(json.dumps(call(command, method, payload), indent=2))
