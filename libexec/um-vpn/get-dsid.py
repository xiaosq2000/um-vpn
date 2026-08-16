#!/usr/bin/env python3
"""Open a Chrome window on the UM SSL VPN portal and harvest the DSID cookie.

The portal federates to ADFS/SAML, so openconnect cannot authenticate on its
own -- a real browser has to complete UMPASS + Duo.  DSID is an HttpOnly
session cookie, so it lives in Chrome's memory and never reliably reaches the
on-disk cookie database.  We therefore read it over the DevTools Protocol,
which sees cookies live.

Prints the cookie value on stdout; everything human-facing goes to stderr.

Only the standard library is used: Ubuntu is PEP-668 managed and this is not
worth a virtualenv, so the ~90 lines of WebSocket below replace a dependency.
"""

import argparse
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

ACTIVE_PORT_FILE = "DevToolsActivePort"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class WebSocket:
    """Minimal RFC 6455 client: localhost, no TLS, no extensions."""

    def __init__(self, host, port, path, timeout=15):
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(f"DevTools refused upgrade: {status!r}")
        self._next_id = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_message(self):
        message = b""
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x9:  # ping
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise ConnectionError("DevTools closed the connection")
            message += payload
            if fin:
                return message.decode()

    def call(self, method, params=None, timeout=15):
        self._next_id += 1
        want = self._next_id
        self._frame(
            1,
            json.dumps(
                {"id": want, "method": method, "params": params or {}}
            ).encode(),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = json.loads(self._recv_message())
            if reply.get("id") != want:
                continue  # an event, or a reply we no longer care about
            if "error" in reply:
                err = reply["error"]
                raise RuntimeError(f"{method}: {err.get('message', err)}")
            return reply.get("result", {})
        raise TimeoutError(f"{method} timed out")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def read_devtools_endpoint(profile):
    """Return (port, browser_ws_path) once Chrome has published them."""
    path = os.path.join(profile, ACTIVE_PORT_FILE)
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return None
    if len(lines) < 2 or not lines[0].isdigit():
        return None
    return int(lines[0]), lines[1]


def devtools_alive(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ):
            return True
    except (urllib.error.URLError, OSError):
        return False


def launch_chrome(browser, profile, url):
    os.makedirs(profile, mode=0o700, exist_ok=True)
    stale = os.path.join(profile, ACTIVE_PORT_FILE)
    if os.path.exists(stale):
        os.remove(stale)
    argv = [
        browser,
        f"--user-data-dir={profile}",
        # Chrome writes the real port to DevToolsActivePort.
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--new-window",
        url,
    ]
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def find_cookie(ws, name, domain):
    for cookie in ws.call("Storage.getCookies").get("cookies", []):
        if cookie.get("name") != name:
            continue
        host = (cookie.get("domain") or "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            return cookie.get("value") or None
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--browser", default="google-chrome")
    ap.add_argument(
        "--profile", required=True, help="dedicated Chrome user-data-dir"
    )
    ap.add_argument("--url", required=True, help="portal URL to open")
    ap.add_argument("--cookie", default="DSID", help="cookie name to harvest")
    ap.add_argument(
        "--domain", required=True, help="domain the cookie must belong to"
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds to wait for login",
    )
    args = ap.parse_args()

    # A window may already be open from a previous run on this profile; Chrome
    # would just hand our launch off to it and exit, so reuse it instead.
    endpoint = read_devtools_endpoint(args.profile)
    proc = None
    if endpoint and devtools_alive(endpoint[0]):
        log("Reusing the login window that is already open.")
    else:
        log(
            f"Opening {args.browser} at {args.url} "
            "-- sign in with UMPASS + Duo."
        )
        log("The window closes by itself once the session cookie appears.")
        proc = launch_chrome(args.browser, args.profile, args.url)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            endpoint = read_devtools_endpoint(args.profile)
            if endpoint and devtools_alive(endpoint[0]):
                break
            if proc.poll() is not None:
                endpoint = read_devtools_endpoint(args.profile)
                if endpoint and devtools_alive(endpoint[0]):
                    break
                log("Chrome exited before the DevTools endpoint came up.")
                return 1
            time.sleep(0.25)
        else:
            log("Timed out waiting for Chrome's DevTools endpoint.")
            return 1

    port, browser_path = endpoint
    ws = WebSocket("127.0.0.1", port, browser_path)

    value = None
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            value = find_cookie(ws, args.cookie, args.domain)
            if value:
                break
            time.sleep(1.0)
        else:
            log(
                f"Gave up after {args.timeout:.0f}s without seeing "
                f"a {args.cookie} cookie."
            )
    except ConnectionError:
        log("The login window was closed before authentication completed.")
        ws.close()
        return 1

    if value:
        log(f"Got the {args.cookie} cookie; closing the login window.")
        # Browser.close is a graceful shutdown, so the ADFS "keep me signed in"
        # cookie is flushed to the profile and the next login can be silent.
        try:
            ws.call("Browser.close", timeout=10)
        except (RuntimeError, TimeoutError, ConnectionError):
            pass
    ws.close()

    if proc is not None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)

    if not value:
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Cancelled.")
        sys.exit(130)
