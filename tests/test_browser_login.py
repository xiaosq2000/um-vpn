"""End-to-end test of the login helper against a stand-in portal.

This drives the real browser-login.py and a real Chrome, headless, but
involves no Duo, no network and no sudo: the "portal" is an http.server on
127.0.0.1 that sets a DSID cookie the way the gateway does. It checks only
what bin/um-vpn relies on -- the cookie alone on stdout, the exit status,
and no Chrome left running -- so a rewrite of the helper should only have to
touch run_helper().

    python3 -m unittest discover -s tests -v    # or: make test
"""

import http.server
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "libexec" / "um-vpn" / "browser-login.py"

# bin/um-vpn probes the same list, so this drives what a user would get.
BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_browser():
    override = os.environ.get("UM_VPN_BROWSER")
    for name in (override,) if override else BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def processes_mentioning(text):
    """PIDs whose command line contains text: here, the test's temp dir."""
    # A substring, not an argument: Chrome rewrites the command line of every
    # process it runs into one space-joined string, children included.
    needle = os.fsencode(text)
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if needle in (proc / "cmdline").read_bytes():
                pids.append(int(proc.name))
        except OSError:
            continue  # exited while we looked
    return pids


class PortalHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request the way the gateway does after a login.

    DSID is HttpOnly with no expiry, like the real one, so the helper has to
    read it live over DevTools. Pulse gateways set other DS* cookies beside
    it; DSLastAccess stands in for those, and must never come back instead.
    """

    def do_GET(self):
        body = b"<!doctype html><title>stand-in portal</title>"
        self.send_response(200)
        self.send_header("Set-Cookie", f"DSLastAccess={int(time.time())}")
        self.send_header(
            "Set-Cookie", f"DSID={self.server.dsid}; Path=/; HttpOnly"
        )
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.visited.set()

    def log_message(self, *args):
        pass  # a line per request would bury the test results


class BrowserLoginTest(unittest.TestCase):
    def setUp(self):
        browser = find_browser()
        if not browser:
            self.skipTest("no Chromium-family browser; set UM_VPN_BROWSER")

        tmp = tempfile.TemporaryDirectory(
            prefix="um-vpn-test-", ignore_cleanup_errors=True
        )
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.profile = self.tmp / "profile"

        # The helper appends its own flags to --browser, so a wrapper is the
        # only way to add --headless without changing the code under test.
        self.browser = self.tmp / "headless-browser"
        self.browser.write_text(
            f'#!/bin/sh\nexec {shlex.quote(browser)} --headless "$@"\n'
        )
        self.browser.chmod(0o755)

        # Cleanups run in reverse, so this one runs before tmp.cleanup:
        # nothing may still be writing to the profile as it is deleted.
        self.addCleanup(self.kill_leftovers)

        self.portal = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), PortalHandler
        )
        self.portal.dsid = secrets.token_hex(16)
        self.portal.visited = threading.Event()
        threading.Thread(target=self.portal.serve_forever, daemon=True).start()
        self.addCleanup(self.portal.server_close)
        self.addCleanup(self.portal.shutdown)

    def run_helper(self, domain, timeout):
        """Run the helper the way bin/um-vpn does, minus the autofill."""
        return subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "--browser",
                str(self.browser),
                "--profile",
                str(self.profile),
                "--url",
                f"http://127.0.0.1:{self.portal.server_port}/",
                "--cookie",
                "DSID",
                "--domain",
                domain,
                "--timeout",
                str(timeout),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            # The login timeout, plus the helper's launch and shutdown waits.
            timeout=timeout + 60,
        )

    def chrome_left_running(self, grace=10):
        """PIDs still using the temp dir once a short grace has passed."""
        deadline = time.monotonic() + grace
        while True:
            pids = processes_mentioning(str(self.tmp))
            if not pids or time.monotonic() >= deadline:
                return pids
            time.sleep(0.2)

    def kill_leftovers(self):
        for pid in processes_mentioning(str(self.tmp)):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def test_prints_the_dsid_and_nothing_else(self):
        result = self.run_helper(domain="127.0.0.1", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        # bin/um-vpn takes the helper's whole stdout as the cookie.
        self.assertEqual(result.stdout, self.portal.dsid + "\n")
        self.assertEqual(self.chrome_left_running(), [])

    def test_ignores_a_dsid_for_another_domain(self):
        result = self.run_helper(domain="example.invalid", timeout=5)
        # Otherwise a Chrome that never loaded the page would pass: the
        # helper has to have been offered the cookie, and refused it.
        self.assertTrue(self.portal.visited.is_set(), result.stderr)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.chrome_left_running(), [])


if __name__ == "__main__":
    unittest.main()
