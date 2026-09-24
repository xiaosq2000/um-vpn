#!/usr/bin/env python3
"""Connect to the University of Macau SSL VPN from a Linux desktop.

The portal hands its login to ADFS and Duo, which openconnect cannot drive
by itself. So um-vpn opens Chrome on the portal, reads the DSID session
cookie over the DevTools protocol once you are signed in, and passes it to
NetworkManager, whose openconnect plugin runs the tunnel without sudo.

Standard library only: installing is copying this file onto PATH.
"""

import argparse
import fcntl
import json
import os
import pwd
import select
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

CONNECTION = "um-vpn"  # the NetworkManager connection's name
DEFAULT_PORTAL = "https://sslvpn.um.edu.mo"
# Chromium-family only: the login is read over the Chrome DevTools Protocol.
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
LOGIN_TIMEOUT = 300  # seconds to get through UMPASS and Duo


class Error(Exception):
    """A failure worth one line to the user, not a traceback."""


class BrowserClosed(Exception):
    """Chrome went away: its window was closed, or it never started."""


def note(message):
    print(message, file=sys.stderr, flush=True)


def first_line(text):
    lines = text.strip().splitlines()
    return lines[0] if lines else "no details"


# --- configuration ---------------------------------------------------------


def portal_url():
    url = os.environ.get("UM_VPN_PORTAL_URL") or DEFAULT_PORTAL
    parts = urllib.parse.urlsplit(url)
    # The URL is also written into NetworkManager's secrets, one per line.
    if parts.scheme != "https" or not parts.hostname or not url.isprintable():
        raise Error(f"UM_VPN_PORTAL_URL is not an https:// URL: {url!r}")
    return url


def profile_dir():
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(data) / "um-vpn" / "chrome"


def find_browser():
    override = os.environ.get("UM_VPN_BROWSER")
    for name in [override] if override else BROWSERS:
        path = shutil.which(name)
        if not path:
            continue
        # Snap confinement keeps Chromium out of hidden directories such as
        # ~/.local/share, so it could never open the login profile.
        if path.startswith("/snap/"):
            raise Error(
                f"{path} is a snap, which cannot use um-vpn's login profile; "
                "install Google Chrome, or set UM_VPN_BROWSER"
            )
        return path
    if override:
        raise Error(f"UM_VPN_BROWSER not found: {override}")
    raise Error("no Chrome or Chromium found; install Google Chrome")


# --- browser login ---------------------------------------------------------


class DevTools:
    """The Chrome DevTools Protocol over --remote-debugging-pipe.

    Chrome reads commands on its fd 3 and answers on its fd 4, one JSON
    message per NUL-terminated record. Unlike a debugging port, a pipe is
    out of reach of every other process on the machine.
    """

    def __init__(self, to_chrome, from_chrome):
        self.to_chrome = to_chrome
        self.from_chrome = from_chrome
        self.pending = b""
        self.last_id = 0

    def call(self, method, timeout=30, **params):
        self.last_id += 1
        message = {"id": self.last_id, "method": method, "params": params}
        try:
            os.write(self.to_chrome, json.dumps(message).encode() + b"\0")
        except BrokenPipeError:
            raise BrowserClosed from None
        deadline = time.monotonic() + timeout
        while True:
            reply = self._receive(deadline)
            if reply.get("id") != self.last_id:
                continue  # an event, or the reply to a call that timed out
            if "error" in reply:
                raise Error(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})

    def _receive(self, deadline):
        while b"\0" not in self.pending:
            wait = max(0, deadline - time.monotonic())
            if not select.select([self.from_chrome], [], [], wait)[0]:
                raise Error("Chrome stopped responding")
            chunk = os.read(self.from_chrome, 65536)
            if not chunk:
                raise BrowserClosed
            self.pending += chunk
        message, self.pending = self.pending.split(b"\0", 1)
        return json.loads(message)

    def close(self):
        os.close(self.to_chrome)
        os.close(self.from_chrome)


def launch_chrome(browser, profile, url):
    """Start Chrome on the portal, with its DevTools pipe on fds 3 and 4."""
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    commands_r, commands_w = os.pipe()
    replies_r, replies_w = os.pipe()

    def move_pipe_to_3_and_4():
        # Runs in the child, just before exec. Both ends are copied above 4
        # first, so neither dup2 can land on the other's original number.
        high_r = fcntl.fcntl(commands_r, fcntl.F_DUPFD, 5)
        high_w = fcntl.fcntl(replies_w, fcntl.F_DUPFD, 5)
        os.dup2(high_r, 3)
        os.dup2(high_w, 4)

    try:
        chrome = subprocess.Popen(
            [
                browser,
                f"--user-data-dir={profile}",
                "--remote-debugging-pipe",
                "--no-first-run",
                "--no-default-browser-check",
                # The tunnel dials the gateway directly, so the login does
                # too, whatever the desktop's proxy setting says.
                "--no-proxy-server",
                "--new-window",
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=move_pipe_to_3_and_4,
            pass_fds=(3, 4),  # keep what move_pipe_to_3_and_4 put there
            start_new_session=True,  # Ctrl-C is ours to handle, not Chrome's
        )
    except OSError as e:
        os.close(commands_w)
        os.close(replies_r)
        raise Error(f"could not start {browser}: {e.strerror}") from None
    finally:
        # Chrome has its own copies now. Holding these would keep EOF from
        # ever arriving when it exits.
        os.close(commands_r)
        os.close(replies_w)
    return chrome, DevTools(commands_w, replies_r)


def close_chrome(chrome, devtools):
    # Browser.close is a clean shutdown, which is what saves ADFS's "keep me
    # signed in" cookie to the profile, so that the next login can be silent.
    try:
        devtools.call("Browser.close", timeout=5)
    except (BrowserClosed, Error):
        pass
    # Chrome also exits once the pipe closes, so the kill below is only for
    # one that has hung.
    devtools.close()
    try:
        chrome.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(chrome.pid, signal.SIGKILL)
        chrome.wait()


def profile_in_use(profile, own_pid=0):
    """True when another Chrome has the profile, e.g. a window left open.

    A second Chrome on the same profile hands its window to the first and
    exits, which from here looks just like the user closing the window.
    """
    try:
        owner = os.readlink(profile / "SingletonLock")  # "<host>-<pid>"
    except OSError:
        return False
    pid = owner.rpartition("-")[2]
    if not pid.isdigit() or int(pid) == own_pid:
        return False
    return Path("/proc", pid).exists()


def host_in_domain(host, domain):
    return host == domain or host.endswith("." + domain)


def find_dsid(devtools, domain):
    """The portal's DSID cookie, read live: it is HttpOnly and never saved."""
    for cookie in devtools.call("Storage.getCookies").get("cookies", []):
        host = cookie.get("domain", "").lstrip(".")
        if (
            cookie.get("name") == "DSID"
            and host_in_domain(host, domain)
            and cookie.get("value")
        ):
            return cookie["value"]
    return None


def browser_login(browser, profile, url, timeout=LOGIN_TIMEOUT):
    """Open the portal in Chrome and return the DSID once the user is in."""
    domain = urllib.parse.urlsplit(url).hostname
    chrome, devtools = launch_chrome(browser, profile, url)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            dsid = find_dsid(devtools, domain)
            if dsid:
                return dsid
            time.sleep(1)
        raise Error(f"no session from {domain} after {timeout}s; gave up")
    except BrowserClosed:
        if profile_in_use(profile, chrome.pid):
            raise Error(
                "Chrome is already running on um-vpn's profile, probably a "
                "login window left open; close it and try again"
            ) from None
        raise Error("the login window closed before you were in") from None
    finally:
        close_chrome(chrome, devtools)


# --- NetworkManager --------------------------------------------------------


def nmcli(*args, pass_fds=()):
    try:
        return subprocess.run(
            ["nmcli", *args], capture_output=True, text=True, pass_fds=pass_fds
        )
    except FileNotFoundError:
        raise Error("nmcli not found; um-vpn needs NetworkManager") from None


def connection_exists():
    show = nmcli("-g", "connection.id", "connection", "show", CONNECTION)
    return show.returncode == 0


def connection_state():
    """'activated', 'activating', ... or None when the tunnel is down."""
    active = nmcli("-t", "-f", "NAME,STATE", "connection", "show", "--active")
    for line in active.stdout.splitlines():
        name, _, state = line.rpartition(":")
        if name == CONNECTION:
            return state
    return None


def create_connection(url):
    user = pwd.getpwuid(os.getuid()).pw_name
    host = urllib.parse.urlsplit(url).hostname
    result = nmcli(
        "connection",
        "add",
        "type",
        "vpn",
        "con-name",
        CONNECTION,
        "vpn-type",
        "openconnect",
        "connection.autoconnect",
        "no",
        # Private to this user, so polkit lets the desktop session manage
        # it without a password.
        "connection.permissions",
        f"user:{user}",
        # flags=2 is "not saved": NetworkManager asks for these on every
        # connect and never writes them to disk.
        "vpn.data",
        f"protocol=nc, gateway={host}, "
        "cookie-flags=2, gateway-flags=2, gwcert-flags=2",
    )
    if result.returncode != 0:
        raise Error(
            "could not create the NetworkManager connection: "
            f"{first_line(result.stderr)}\n"
            "Is network-manager-openconnect installed?"
        )
    note(f"Created the NetworkManager connection '{CONNECTION}'.")


def connect(dsid, url):
    """Hand the cookie to NetworkManager and wait for the tunnel."""
    if not dsid.isprintable():
        raise Error("the portal's cookie has unexpected characters")
    # nmcli reads these from a file; a pipe keeps the cookie off the disk
    # and out of every process's argv. All four are given because nmcli
    # drops the whole request over a missing one, and GNOME then opens its
    # own login dialog. Empty gwcert and resolve mean the usual certificate
    # check and DNS lookup.
    secrets = (
        f"vpn.secrets.cookie:DSID={dsid}\n"
        f"vpn.secrets.gateway:{url}\n"
        "vpn.secrets.gwcert:\n"
        "vpn.secrets.resolve:\n"
    )
    read_end, write_end = os.pipe()
    os.write(write_end, secrets.encode())
    os.close(write_end)
    started = time.time()
    try:
        result = nmcli(
            "--wait",
            "60",
            "connection",
            "up",
            CONNECTION,
            "passwd-file",
            f"/dev/fd/{read_end}",
            pass_fds=(read_end,),
        )
    finally:
        os.close(read_end)
    if result.returncode != 0:
        raise Error(
            f"NetworkManager could not connect: {first_line(result.stderr)}"
            + openconnect_log(started)
        )


def openconnect_log(since):
    """openconnect's own account of a failure; nmcli rarely gives one."""
    try:
        log = subprocess.run(
            [
                "journalctl",
                "--no-pager",
                "--output=cat",
                f"--since=@{int(since)}",
                "_COMM=openconnect",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    lines = log.stdout.strip().splitlines()[-5:]
    if not lines:
        return "\nDetails: journalctl -b _COMM=openconnect"
    return "".join(f"\n  openconnect: {line}" for line in lines)


# --- commands --------------------------------------------------------------


def cmd_status():
    state = connection_state()
    if state is None:
        print("um-vpn: disconnected")
        return
    if state != "activated":
        print(f"um-vpn: {state}")
        return
    # Only the address: for a VPN, NetworkManager's GENERAL.IP-IFACE is the
    # Wi-Fi or Ethernet device underneath, not the tunnel. Several addresses
    # come back joined by " | ".
    show = nmcli("-g", "IP4.ADDRESS", "connection", "show", CONNECTION)
    address = show.stdout.split("|")[0].strip()
    print(f"um-vpn: connected, {address}" if address else "um-vpn: connected")


def cmd_on():
    if connection_state() is not None:
        cmd_status()
        return
    url = portal_url()
    browser = find_browser()
    # Before the login, so a missing plugin surfaces before a Duo push.
    if not connection_exists():
        create_connection(url)
    note("Opening the portal in Chrome: sign in with UMPASS and approve Duo.")
    note("The window closes by itself once you are in.")
    dsid = browser_login(browser, profile_dir(), url)
    connect(dsid, url)
    cmd_status()


def cmd_off():
    if connection_state() is None:
        note("Not connected.")
        return
    result = nmcli("connection", "down", CONNECTION)
    if result.returncode != 0:
        raise Error(f"could not disconnect: {first_line(result.stderr)}")
    # openconnect logs the session out on its way down, so the next connect
    # is a fresh login: usually a window that closes by itself.
    note("Disconnected.")


def cmd_forget():
    profile = profile_dir()
    if profile_in_use(profile):
        raise Error("a login window is still open; close it first")
    removed = []
    if connection_exists():
        # Deleting an active connection disconnects it first.
        result = nmcli("connection", "delete", CONNECTION)
        if result.returncode != 0:
            raise Error(
                f"could not delete the connection: {first_line(result.stderr)}"
            )
        removed.append(f"the NetworkManager connection '{CONNECTION}'")
    if profile.exists():
        shutil.rmtree(profile)
        removed.append(f"the login profile {profile}")
    note(f"Removed {' and '.join(removed)}." if removed else "Nothing to do.")


COMMANDS = {
    "on": cmd_on,
    "off": cmd_off,
    "status": cmd_status,
    "forget": cmd_forget,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="um-vpn",
        usage="%(prog)s [-h] command",
        description="Connect to the University of Macau SSL VPN.",
        epilog="Environment: UM_VPN_PORTAL_URL (default "
        f"{DEFAULT_PORTAL}), UM_VPN_BROWSER (a Chrome or Chromium to use).",
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser("on", help="sign in through Chrome and connect")
    commands.add_parser("off", help="disconnect, ending the portal session")
    commands.add_parser("status", help="show whether the tunnel is up")
    commands.add_parser(
        "forget",
        help="delete the NetworkManager connection and the login profile",
    )
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        COMMANDS[args.command]()
    except Error as e:
        note(f"um-vpn: {e}")
        return 1
    except KeyboardInterrupt:
        note("um-vpn: cancelled")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
