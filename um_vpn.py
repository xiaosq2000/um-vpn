#!/usr/bin/env python3
"""Connect to the University of Macau SSL VPN from a Linux desktop.

The portal hands its login to ADFS and Duo, which openconnect cannot drive
by itself. So um-vpn opens Chrome on the portal, reads the DSID session
cookie over the DevTools protocol once you are signed in, and passes it to
NetworkManager, whose openconnect plugin runs the tunnel without sudo. With
your UMPASS ID and password in the login keyring, it also fills in the
sign-in form, which leaves only Duo to you.

Standard library only: installing is copying this file onto PATH.
"""

import argparse
import fcntl
import getpass
import ipaddress
import json
import os
import pwd
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# The NetworkManager connection's name: GNOME shows it under VPN, and um-vpn
# finds the connection by it.
CONNECTION = "University of Macau"
DEFAULT_PORTAL = "https://sslvpn.um.edu.mo"
# Chromium-family only: the login is read over the Chrome DevTools Protocol.
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
LOGIN_TIMEOUT = 300  # seconds to get through UMPASS and Duo
PAGE_TIMEOUT = 5  # seconds; a tab that is closing may never answer
KEEPALIVE_INTERVAL = 60  # seconds; an idle tunnel dies at about 300
DNS_PORT = 53
# The login keyring items that hold the UMPASS sign-in, one per field, so
# that `secret-tool lookup` returns one secret and nothing beside it.
KEYRING = ("service", "um-vpn")


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


# --- stored sign-in --------------------------------------------------------


def secret_tool(*args, stdin=""):
    """Run secret-tool, or return None when it is not installed.

    stdin is always a pipe: on a terminal, secret-tool would prompt there.
    """
    try:
        return subprocess.run(
            ["secret-tool", *args], input=stdin.encode(), capture_output=True
        )
    except FileNotFoundError:
        return None


def keyring_get(field):
    """A stored value, or None: no item, no keyring, or no secret-tool."""
    result = secret_tool("lookup", *KEYRING, "field", field)
    if result is None or result.returncode != 0 or not result.stdout:
        return None
    # Exactly as stored: secret-tool adds a newline only on a terminal, and
    # a password that comes back changed is a failed UMPASS attempt.
    try:
        return result.stdout.decode()
    except UnicodeDecodeError:
        return None


def keyring_set(field, label, value):
    result = secret_tool(
        "store", f"--label={label}", *KEYRING, "field", field, stdin=value
    )
    if result is None:
        raise Error("secret-tool not found; install libsecret-tools")
    if result.returncode != 0:
        details = first_line(result.stderr.decode(errors="replace"))
        raise Error(f"could not write to the keyring: {details}")


def keyring_clear():
    """Delete the stored sign-in; True when there was one to delete."""
    result = secret_tool("clear", *KEYRING)
    return result is not None and result.returncode == 0


def stored_sign_in():
    """The stored (ID, password), or None."""
    user = keyring_get("username")
    # Only once there is an ID: each lookup can raise the keyring's unlock
    # prompt.
    password = keyring_get("password") if user else None
    return (user, password) if password else None


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

    def call(self, method, timeout=30, session=None, **params):
        self.last_id += 1
        message = {"id": self.last_id, "method": method, "params": params}
        # A call to a tab goes down the same pipe, tagged with the session
        # that um-vpn opened on the tab.
        if session:
            message["sessionId"] = session
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
    # Browser.close is a clean shutdown, which is what saves Duo's
    # remembered device to the profile, so that the next login can skip Duo.
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


def login_domain(host):
    """Where the sign-in may be typed: the portal's host minus one label.

    sslvpn.um.edu.mo gives um.edu.mo, which holds UM's ADFS. None for an IP
    address, and for a single label, which would take in a whole TLD.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        domain = host.partition(".")[2]
        return domain if "." in domain else None
    return None


# Runs in the tab, in a world of um-vpn's own that the page's scripts cannot
# reach. Returns [state, page], where page tells one document from the next.
FILL_SIGN_IN = """\
function (scheme, host, user, password) {
    "use strict";
    // location's fields cannot be redefined and === runs no page code, so
    // this holds even in the page's own world, where the call can land if
    // a new renderer process reuses the context id it names.
    if (location.protocol !== scheme || location.hostname !== host) {
        return ["elsewhere", 0];
    }
    const page = performance.timeOrigin;
    // Until then, the handler behind the button may be missing.
    if (document.readyState !== "complete") {
        return ["loading", page];
    }
    const shown = (id) => {
        const element = document.getElementById(id);
        return element && element.getClientRects().length ? element : null;
    };
    const name = shown("userNameInput");
    const secret = shown("passwordInput");
    const button = shown("submitButton");
    if (!document.getElementById("loginForm") || !name || !secret
            || !button) {
        return ["other", page];  // Duo, the portal, or a changed page
    }
    const error = document.getElementById("errorText");
    if (error && error.textContent.trim()) {
        return ["error", page];
    }
    if (typeof password !== "string") {
        return ["form", page];
    }
    name.value = user;
    secret.value = password;
    // The button, not form.submit(): its handler is UM's own, which turns
    // a bare ID into ID@um.edu.mo. A page's handlers run in the page's
    // world, even for a click from this one.
    button.click();
    return ["submitted", page];
}
"""


def call_in_page(devtools, tab, function, *args):
    """Call a JavaScript function in a tab; None when it throws.

    It runs in an isolated world, whose built-ins the page cannot replace.
    The arguments travel as values, never as script text.
    """
    session = devtools.call(
        "Target.attachToTarget",
        timeout=PAGE_TIMEOUT,
        targetId=tab,
        flatten=True,
    )["sessionId"]
    try:
        tree = devtools.call(
            "Page.getFrameTree", timeout=PAGE_TIMEOUT, session=session
        )
        world = devtools.call(
            "Page.createIsolatedWorld",
            timeout=PAGE_TIMEOUT,
            session=session,
            frameId=tree["frameTree"]["frame"]["id"],
            # Named, so that a document gets one world however many calls.
            worldName="um-vpn",
        )
        reply = devtools.call(
            "Runtime.callFunctionOn",
            timeout=PAGE_TIMEOUT,
            session=session,
            functionDeclaration=function,
            executionContextId=world["executionContextId"],
            arguments=[{"value": arg} for arg in args],
            returnByValue=True,
        )
    finally:
        try:
            devtools.call(
                "Target.detachFromTarget",
                timeout=PAGE_TIMEOUT,
                sessionId=session,
            )
        except Error:
            pass
    if "exceptionDetails" in reply:
        return None  # and never shown, in case it quotes an argument
    return reply.get("result", {}).get("value")


class SignIn:
    """Types the stored UMPASS ID and password into ADFS's form, once.

    UMPASS locks the account after a few wrong passwords, and the account
    holds email too. So the password goes to the page in one call per
    login, whatever comes of it; a form that already shows an error is left
    alone; and a password that UMPASS refuses is forgotten.
    """

    def __init__(self, devtools, url, user, password):
        parts = urllib.parse.urlsplit(url)
        self.devtools = devtools
        self.scheme = parts.scheme
        self.domain = login_domain(parts.hostname or "")
        self.user = user
        self.password = password
        self.watching = None  # (tab, page) once the password has gone out
        self.done = self.domain is None

    def step(self):
        """Take one more look at the tabs; called at every poll."""
        if self.done:
            return
        try:
            tabs = self.tabs()
        except Error:
            return  # again at the next poll
        if self.watching:
            self.watch(tabs)
        else:
            self.wait(tabs)

    def tabs(self):
        """{tab: host} for the tabs whose page may be given the sign-in."""
        tabs = {}
        for target in self.devtools.call("Target.getTargets")["targetInfos"]:
            url = urllib.parse.urlsplit(target["url"])
            host = url.hostname or ""
            if (
                target["type"] == "page"
                and url.scheme == self.scheme
                and host_in_domain(host, self.domain)
            ):
                tabs[target["targetId"]] = host
        return tabs

    def ask(self, tab, host, password=None):
        """The form's [state, page] in a tab; [None, None] when unknown."""
        try:
            answer = call_in_page(
                self.devtools,
                tab,
                FILL_SIGN_IN,
                self.scheme + ":",
                host,
                self.user if password else None,
                password,
            )
        except Error:
            answer = None
        return answer or [None, None]

    def wait(self, tabs):
        for tab, host in tabs.items():
            state, page = self.ask(tab, host)
            if state == "error":
                self.stop(
                    "The sign-in page already shows an error; sign in by hand."
                )
                return
            if state == "form":
                self.submit(tab, host, page)
                return

    def submit(self, tab, host, page):
        # Dropped before the call, so that nothing can send it twice.
        password, self.password = self.password, None
        state, sent = self.ask(tab, host, password)
        if state == "submitted":
            note("Filled in the stored UMPASS sign-in.")
            self.watching = (tab, sent)
        elif state is None:
            # The call failed, but the form may have gone all the same.
            self.watching = (tab, page)
        else:
            self.stop("Could not fill in the sign-in; sign in by hand.")

    def watch(self, tabs):
        tab, page = self.watching
        if tab not in tabs:
            self.stop()  # closed, or gone to Duo: past the password
            return
        state, now = self.ask(tab, tabs[tab])
        if state in (None, "elsewhere", "loading"):
            return
        if now == page:
            if state == "error":
                self.refused()
            return  # still the page that sent the form
        if state in ("form", "error"):
            self.refused()  # the sign-in came back
        else:
            self.stop()  # Duo, or on to the portal: past the password

    def refused(self):
        keyring_clear()
        self.stop(
            "UMPASS did not accept the stored ID and password, so um-vpn "
            "has forgotten them.",
            "Finish in the window, then run 'um-vpn remember'.",
        )

    def stop(self, *lines):
        for line in lines:
            note(line)
        self.done = True


def browser_login(browser, profile, url, timeout=LOGIN_TIMEOUT, sign_in=None):
    """Open the portal in Chrome and return the DSID once the user is in.

    sign_in, a stored (ID, password), goes into the UMPASS form.
    """
    domain = urllib.parse.urlsplit(url).hostname
    chrome, devtools = launch_chrome(browser, profile, url)
    form = SignIn(devtools, url, *sign_in) if sign_in else None
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            dsid = find_dsid(devtools, domain)
            if dsid:
                return dsid
            if form:
                form.step()
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
        # disable_udp turns ESP off, so everything crosses the TLS
        # connection that the keepalive holds open. flags=2 is "not saved":
        # NetworkManager asks for these on every connect and never writes
        # them to disk.
        "vpn.data",
        f"protocol=nc, gateway={host}, disable_udp=yes, "
        "cookie-flags=2, gateway-flags=2, gwcert-flags=2",
    )
    if result.returncode != 0:
        raise Error(
            "could not create the NetworkManager connection: "
            f"{first_line(result.stderr)}\n"
            "Is network-manager-openconnect installed?"
        )
    note(f"Created the NetworkManager connection '{CONNECTION}'.")


def disable_esp():
    """Turn ESP off on a connection made before um-vpn did so itself."""
    result = nmcli(
        "connection", "modify", CONNECTION, "+vpn.data", "disable_udp=yes"
    )
    if result.returncode != 0:
        raise Error(
            f"could not update the connection: {first_line(result.stderr)}"
        )


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


# --- keepalive -------------------------------------------------------------


def tunnel_dns():
    """The DNS servers the gateway pushed, none once the tunnel is down.

    They are inside the tunnel, so a query to one has to cross it.
    """
    show = nmcli("-g", "IP4.DNS", "connection", "show", CONNECTION)
    return show.stdout.replace("|", " ").split()


def dns_query(name):
    """A DNS query for name's address, as it goes on the wire."""
    header = os.urandom(2) + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    labels = b"".join(
        bytes([len(label)]) + label for label in name.encode().split(b".")
    )
    return header + labels + b"\0" + struct.pack(">HH", 1, 1)


def poke_tunnel(server, name):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        try:
            sock.sendto(dns_query(name), (server, DNS_PORT))
            sock.recv(512)  # only so the answer has somewhere to land
        except OSError:
            pass  # the query crossed the tunnel, answered or not


def keepalive_lock():
    """An exclusive lock on a file, or None when another process has it."""
    path = profile_dir().parent / "keepalive.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def start_keepalive():
    # Detached, so it outlives this command and the terminal's Ctrl-C.
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "keepalive"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        start_new_session=True,
    )


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
    if connection_exists():
        disable_esp()
    else:
        create_connection(url)
    sign_in = stored_sign_in()
    if sign_in:
        note(
            "Opening the portal in Chrome: um-vpn fills in your UMPASS "
            "sign-in; approve Duo if it asks."
        )
    else:
        note(
            "Opening the portal in Chrome: sign in with UMPASS and approve "
            "Duo."
        )
    note("The window closes by itself once you are in.")
    dsid = browser_login(browser, profile_dir(), url, sign_in=sign_in)
    connect(dsid, url)
    start_keepalive()
    cmd_status()


def cmd_off():
    if connection_state() is None:
        note("Not connected.")
        return
    result = nmcli("connection", "down", CONNECTION)
    if result.returncode != 0:
        raise Error(f"could not disconnect: {first_line(result.stderr)}")
    # openconnect logs the session out on its way down, so the next connect
    # is a fresh login, which um-vpn fills in from the keyring.
    note("Disconnected.")


def cmd_remember():
    if not shutil.which("secret-tool"):
        raise Error("secret-tool not found; install libsecret-tools")
    if not sys.stdin.isatty():
        raise Error("remember reads the password from a terminal")
    try:
        user = input("UMPASS ID: ").strip()
        password = getpass.getpass("UMPASS password: ")
    except EOFError:
        raise Error("nothing stored") from None
    if not user or not password:
        raise Error("nothing stored: it takes both an ID and a password")
    # Cleared first, so that a store failing halfway leaves an ID on its
    # own, which is ignored, and never a new ID with an old password.
    keyring_clear()
    keyring_set("username", "um-vpn: UMPASS ID", user)
    keyring_set("password", "um-vpn: UMPASS password", password)
    note("Stored in the login keyring.")
    note("um-vpn on now fills in the UMPASS sign-in; approve Duo if it asks.")


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
    if keyring_clear():
        removed.append("the stored UMPASS sign-in")
    for item in removed:
        note(f"Removed {item}.")
    if not removed:
        note("Nothing to do.")


def cmd_keepalive():
    """Query the tunnel's DNS every minute until the tunnel is down.

    'on' starts this in the background. openconnect sends nothing on the
    TLS connection of an idle tunnel, and something between here and the
    gateway drops it after about five minutes without a word.
    """
    name = urllib.parse.urlsplit(portal_url()).hostname
    lock = keepalive_lock()
    if lock is None:
        return  # one is already running
    with lock:
        while connection_state() is not None:
            servers = tunnel_dns()
            if servers:
                poke_tunnel(servers[0], name)
            time.sleep(KEEPALIVE_INTERVAL)


COMMANDS = {
    "on": cmd_on,
    "off": cmd_off,
    "status": cmd_status,
    "remember": cmd_remember,
    "forget": cmd_forget,
    "keepalive": cmd_keepalive,
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
        "remember",
        help="store your UMPASS ID and password in the login keyring",
    )
    commands.add_parser(
        "forget",
        help="delete the NetworkManager connection, the login profile and "
        "the stored sign-in",
    )
    # Started by 'on'. Without a help= it stays out of the help.
    commands.add_parser("keepalive")
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
