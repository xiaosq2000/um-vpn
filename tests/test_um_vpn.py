"""Tests for um-vpn that need no Duo, no network and no sudo.

The login tests drive a real headless Chrome against a stand-in portal on
127.0.0.1. The command tests put a fake nmcli on PATH that records every
call, so what reaches NetworkManager -- and what never reaches an argv --
is checked through the real pipes.
"""

import http.server
import json
import os
import secrets
import shlex
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

import um_vpn

# --- login ------------------------------------------------------------------


class PortalHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request the way the gateway does after a login.

    DSID is HttpOnly with no expiry, like the real one, so it can only be
    read live over DevTools. Pulse gateways set other DS* cookies beside it;
    DSLastAccess stands in for those, and must never come back instead.
    """

    def do_GET(self):
        body = b"<!doctype html><title>stand-in portal</title>"
        self.send_response(200)
        self.send_header("Set-Cookie", f"DSLastAccess={int(time.time())}")
        if self.server.dsid:
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


@pytest.fixture
def portal():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
    server.dsid = secrets.token_hex(16)
    server.visited = threading.Event()
    server.url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def processes_mentioning(text):
    """PIDs whose command line contains text: here, a test's temp dir."""
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


def chrome_left_running(tmp_path, grace=10):
    """PIDs still using tmp_path once a short grace has passed."""
    deadline = time.monotonic() + grace
    while True:
        pids = processes_mentioning(str(tmp_path))
        if not pids or time.monotonic() >= deadline:
            return pids
        time.sleep(0.2)


@pytest.fixture
def headless_chrome(tmp_path):
    """The real Chrome, run headless through a wrapper script."""
    try:
        browser = um_vpn.find_browser()
    except um_vpn.Error as e:
        pytest.skip(str(e))
    # um-vpn passes its own flags, so a wrapper is the one way to add
    # --headless without changing the code under test.
    wrapper = tmp_path / "headless-chrome"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shlex.quote(browser)} --headless "$@"\n'
    )
    wrapper.chmod(0o755)
    yield str(wrapper)
    for pid in processes_mentioning(str(tmp_path)):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_login_returns_the_dsid_and_closes_chrome(
    portal, headless_chrome, tmp_path
):
    profile = tmp_path / "profile"
    dsid = um_vpn.browser_login(headless_chrome, profile, portal.url, 30)
    assert dsid == portal.dsid
    assert chrome_left_running(tmp_path) == []


def test_login_gives_up_without_a_dsid_and_closes_chrome(
    portal, headless_chrome, tmp_path
):
    portal.dsid = None
    with pytest.raises(um_vpn.Error, match="no session"):
        um_vpn.browser_login(
            headless_chrome, tmp_path / "profile", portal.url, 5
        )
    # Otherwise a Chrome that never loaded the page would pass as well.
    assert portal.visited.is_set()
    assert chrome_left_running(tmp_path) == []


def exits_at_once(tmp_path):
    """A 'browser' that quits before answering, like a closed window."""
    browser = tmp_path / "exits-at-once"
    browser.write_text("#!/bin/sh\nexit 0\n")
    browser.chmod(0o755)
    return str(browser)


def test_login_reports_a_window_closed_early(tmp_path):
    with pytest.raises(um_vpn.Error, match="window closed"):
        um_vpn.browser_login(
            exits_at_once(tmp_path), tmp_path / "profile", "http://x/", 5
        )


def test_chrome_gets_its_own_profile_the_pipe_and_no_proxy(tmp_path):
    browser = tmp_path / "records-its-argv"
    browser.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >{tmp_path}/argv\n')
    browser.chmod(0o755)
    with pytest.raises(um_vpn.Error):
        um_vpn.browser_login(str(browser), tmp_path / "p", "https://x/", 5)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert f"--user-data-dir={tmp_path / 'p'}" in argv
    assert "--remote-debugging-pipe" in argv
    # The tunnel goes direct, so the login must too, or a desktop proxy
    # would sign in from a different address than the tunnel connects from.
    assert "--no-proxy-server" in argv
    assert argv[-1] == "https://x/"


def test_login_names_a_window_left_open_on_the_profile(tmp_path):
    # Chrome records its owner as "<host>-<pid>"; this process stands in for
    # the Chrome that already has the profile.
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    with pytest.raises(um_vpn.Error, match="already running"):
        um_vpn.browser_login(exits_at_once(tmp_path), profile, "http://x/", 5)


class FakeDevTools:
    def __init__(self, cookies):
        self.cookies = cookies

    def call(self, method, **params):
        assert method == "Storage.getCookies"
        return {"cookies": self.cookies}


def test_find_dsid_takes_only_the_portal_s_own_cookie():
    portal = "sslvpn.um.edu.mo"
    cookies = [
        {"name": "DSID", "domain": "evil-sslvpn.um.edu.mo", "value": "no-1"},
        {"name": "DSID", "domain": "um.edu.mo", "value": "no-2"},
        {"name": "DSLastAccess", "domain": portal, "value": "no-3"},
        {"name": "DSID", "domain": portal, "value": ""},
        {"name": "DSID", "domain": "." + portal, "value": "yes"},
    ]
    assert um_vpn.find_dsid(FakeDevTools(cookies), portal) == "yes"
    assert um_vpn.find_dsid(FakeDevTools(cookies[:4]), portal) is None


# --- configuration ----------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://sslvpn.um.edu.mo", "https://", "https://a.b\nX:y"]
)
def test_portal_url_must_be_one_line_of_https(monkeypatch, url):
    monkeypatch.setenv("UM_VPN_PORTAL_URL", url)
    with pytest.raises(um_vpn.Error):
        um_vpn.portal_url()


def test_a_snap_browser_is_refused_up_front(monkeypatch):
    monkeypatch.delenv("UM_VPN_BROWSER", raising=False)
    monkeypatch.setattr(um_vpn.shutil, "which", lambda n: f"/snap/bin/{n}")
    with pytest.raises(um_vpn.Error, match="snap"):
        um_vpn.find_browser()


# --- commands, against a fake nmcli -----------------------------------------

# Answers the calls um-vpn makes the way nmcli 1.54 does, and records each
# one, with the contents of any passwd-file, to FAKE_NMCLI_LOG.
FAKE_NMCLI = """\
import json, os, sys

args = sys.argv[1:]
state_file = os.environ["FAKE_NMCLI_STATE"]
with open(state_file) as f:
    state = json.load(f)
call = {"argv": args}
if "passwd-file" in args:
    with open(args[args.index("passwd-file") + 1]) as f:
        call["passwd_file"] = f.read()
with open(os.environ["FAKE_NMCLI_LOG"], "a") as f:
    f.write(json.dumps(call) + "\\n")


def reply(out="", err="", code=0):
    with open(state_file, "w") as f:
        json.dump(state, f)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


missing = ("", "Error: unknown connection 'University of Macau'.\\n", 10)
if "--active" in args:
    ours = "University of Macau:activated\\n" if state["active"] else ""
    reply("Wired connection 1:activated\\n" + ours)
if "add" in args:
    state["exists"] = True
    reply("Connection 'University of Macau' (1234) successfully added.\\n")
if "up" in args:
    if state["fail_up"]:
        failed = "Error: Connection activation failed: Unknown reason\\n"
        hint = "Hint: use 'journalctl -xe NM_CONNECTION=1234'\\n"
        reply("", failed + hint, 4)
    state["active"] = True
    reply("Connection successfully activated\\n")
if "down" in args:
    if not state["active"]:
        reply(*missing)
    state["active"] = False
    reply("Connection 'University of Macau' successfully deactivated\\n")
if "delete" in args:
    if not state["exists"]:
        reply(*missing)
    state.update(exists=False, active=False)
    reply("Connection 'University of Macau' (1234) successfully deleted.\\n")
if "modify" in args:
    reply()
if "IP4.ADDRESS" in args:
    reply("192.0.2.10/32 | 10.0.0.1/8\\n" if state["active"] else "")
if "IP4.DNS" in args:
    reply("127.0.0.1 | 127.0.0.2\\n" if state["active"] else "")
if "connection.id" in args:
    reply("University of Macau\\n") if state["exists"] else reply(*missing)
reply("", "fake nmcli: unexpected call\\n", 2)
"""


class FakeNetworkManager:
    def __init__(self, tmp_path):
        self.state_file = tmp_path / "nm-state.json"
        self.log_file = tmp_path / "nm-calls.jsonl"
        self.set(exists=False, active=False, fail_up=False)

    def set(self, **changes):
        state = {}
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text())
        state.update(changes)
        self.state_file.write_text(json.dumps(state))

    @property
    def state(self):
        return json.loads(self.state_file.read_text())

    @property
    def calls(self):
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text().splitlines()
        return [json.loads(line) for line in lines]

    def called(self, word):
        return [call for call in self.calls if word in call["argv"]]


@pytest.fixture
def nm(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nmcli = bin_dir / "nmcli"
    nmcli.write_text(f"#!{sys.executable}\n{FAKE_NMCLI}")
    # What openconnect says when a cookie is refused; nmcli only says
    # "Unknown reason".
    journalctl = bin_dir / "journalctl"
    journalctl.write_text(
        "#!/bin/sh\necho 'Cookie was rejected by server; exiting.'\n"
    )
    for tool in (nmcli, journalctl):
        tool.chmod(0o755)
    fake = FakeNetworkManager(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NMCLI_STATE", str(fake.state_file))
    monkeypatch.setenv("FAKE_NMCLI_LOG", str(fake.log_file))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("UM_VPN_PORTAL_URL", raising=False)
    monkeypatch.setattr(um_vpn, "find_browser", lambda: "/usr/bin/true")
    # A real keepalive would outlive the test by a minute.
    fake.keepalives = []
    monkeypatch.setattr(
        um_vpn, "start_keepalive", lambda: fake.keepalives.append("started")
    )
    return fake


@pytest.fixture
def login(monkeypatch):
    """Stands in for the browser login; records the URLs it was sent to."""
    urls = []

    def fake_login(browser, profile, url, timeout=None):
        urls.append(url)
        return "fake-dsid"

    monkeypatch.setattr(um_vpn, "browser_login", fake_login)
    return urls


def test_on_creates_the_connection_and_pipes_in_the_cookie(nm, login, capsys):
    assert um_vpn.main(["on"]) == 0

    [add] = nm.called("add")
    # um-vpn finds its connection by name, so a new name would leave every
    # existing connection behind, still listed in GNOME.
    assert add["argv"][add["argv"].index("con-name") + 1] == (
        "University of Macau"
    )
    # disable_udp: with ESP on, the keepalive's queries skip the TLS
    # connection, which then dies after five idle minutes.
    assert add["argv"][add["argv"].index("vpn.data") + 1] == (
        "protocol=nc, gateway=sslvpn.um.edu.mo, disable_udp=yes, "
        "cookie-flags=2, gateway-flags=2, gwcert-flags=2"
    )
    assert "connection.permissions" in add["argv"]
    assert login == ["https://sslvpn.um.edu.mo"]
    [up] = nm.called("up")
    assert up["passwd_file"] == (
        "vpn.secrets.cookie:DSID=fake-dsid\n"
        "vpn.secrets.gateway:https://sslvpn.um.edu.mo\n"
        "vpn.secrets.gwcert:\n"
        "vpn.secrets.resolve:\n"
    )
    # The cookie is a live session: ps shows every argv to every user.
    assert not any("fake-dsid" in " ".join(c["argv"]) for c in nm.calls)
    assert nm.keepalives == ["started"]
    assert capsys.readouterr().out == "um-vpn: connected, 192.0.2.10/32\n"


def test_on_reuses_an_existing_connection_with_esp_off(nm, login):
    nm.set(exists=True)
    assert um_vpn.main(["on"]) == 0
    assert nm.called("add") == []
    # Connections made by earlier versions have ESP on.
    [modify] = nm.called("modify")
    assert modify["argv"][-2:] == ["+vpn.data", "disable_udp=yes"]
    [up] = nm.called("up")
    assert nm.calls.index(modify) < nm.calls.index(up)
    assert nm.state["active"]
    assert nm.keepalives == ["started"]


def test_on_while_connected_only_reports(nm, login, capsys):
    nm.set(exists=True, active=True)
    assert um_vpn.main(["on"]) == 0
    assert login == []
    assert "connected" in capsys.readouterr().out


def test_a_failed_connect_quotes_openconnect(nm, login, capsys):
    nm.set(exists=True, fail_up=True)
    assert um_vpn.main(["on"]) == 1
    err = capsys.readouterr().err
    assert "Unknown reason" in err
    assert "Cookie was rejected by server" in err


def test_bare_um_vpn_shows_help_and_touches_nothing(nm, login, capsys):
    nm.set(exists=True, active=True)
    assert um_vpn.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: um-vpn")
    for command in ("on", "off", "status", "forget"):
        assert command in out
    assert "keepalive" not in out  # only 'on' has a use for it
    # Running it to see what it does must not cost a Duo push or the tunnel.
    assert nm.calls == []
    assert login == []


def test_off_when_disconnected_changes_nothing(nm, capsys):
    assert um_vpn.main(["off"]) == 0
    assert nm.called("down") == []
    assert "Not connected" in capsys.readouterr().err


def test_forget_removes_the_connection_and_the_profile(nm, tmp_path):
    nm.set(exists=True, active=True)
    profile = um_vpn.profile_dir()
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("")
    assert um_vpn.main(["forget"]) == 0
    assert not nm.state["exists"]
    assert not profile.exists()
    assert um_vpn.main(["forget"]) == 0  # and again, with nothing left


# --- keepalive --------------------------------------------------------------

# The nm fixture stubs this out; one test runs the real thing.
START_KEEPALIVE = um_vpn.start_keepalive


def finishes(target, timeout=30):
    """Runs target in a thread; False if it is still going after timeout."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive()


def test_the_keepalive_queries_the_tunnel_dns_until_the_tunnel_is_down(
    nm, monkeypatch
):
    nm.set(exists=True, active=True)
    monkeypatch.setattr(um_vpn, "KEEPALIVE_INTERVAL", 0)
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    monkeypatch.setattr(um_vpn, "DNS_PORT", server.getsockname()[1])
    queries = []

    def answer_twice_then_drop_the_tunnel():
        while len(queries) < 2:
            query, client = server.recvfrom(512)
            queries.append(query)
            if len(queries) == 2:
                nm.set(active=False)
            server.sendto(query, client)

    threading.Thread(target=answer_twice_then_drop_the_tunnel).start()
    assert finishes(um_vpn.cmd_keepalive)
    server.close()
    assert len(queries) == 2
    # An ordinary question, about the portal's own name.
    assert b"\x06sslvpn\x02um\x03edu\x02mo\x00" in queries[0]


def test_a_second_keepalive_leaves_at_once(nm):
    nm.set(exists=True, active=True)
    with um_vpn.keepalive_lock():
        assert finishes(um_vpn.cmd_keepalive, timeout=10)
    assert nm.calls == []


def test_the_keepalive_runs_this_file_in_a_process_of_its_own(nm):
    child = START_KEEPALIVE()
    assert child.args == [sys.executable, um_vpn.__file__, "keepalive"]
    # The fake tunnel is down, so it asks NetworkManager once and exits.
    assert child.wait(timeout=30) == 0
    assert nm.called("--active")
