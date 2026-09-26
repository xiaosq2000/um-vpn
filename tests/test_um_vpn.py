"""Tests for um-vpn that need no Duo, no network and no sudo.

The login tests drive a real headless Chrome against a stand-in portal on
127.0.0.1. The command tests put a fake nmcli on PATH that records every
call, so what reaches NetworkManager -- and what never reaches an argv --
is checked through the real pipes. Every test gets a fake secret-tool too,
so that none can read or delete a real stored password.
"""

import http.server
import io
import json
import os
import secrets
import shlex
import signal
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

import um_vpn

# --- a fake keyring, for every test -----------------------------------------

# Keeps items in FAKE_KEYRING and logs each call, with its stdin, to
# FAKE_SECRET_TOOL_LOG. Answers as libsecret's secret-tool does: a lookup
# prints the secret with no newline, and a lookup or clear that matches
# nothing exits 1.
FAKE_SECRET_TOOL = """\
import json, os, sys

action, *rest = sys.argv[1:]
attributes = [arg for arg in rest if not arg.startswith("--")]
wanted = dict(zip(attributes[::2], attributes[1::2]))
stdin = sys.stdin.read()
with open(os.environ["FAKE_SECRET_TOOL_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\\n")
with open(os.environ["FAKE_KEYRING"]) as f:
    items = json.load(f)
matching = [
    item
    for item in items
    if all(item["attributes"].get(k) == v for k, v in wanted.items())
]
code = 0
if action == "store":
    items = [item for item in items if item["attributes"] != wanted]
    items.append({"attributes": wanted, "secret": stdin})
elif action == "lookup" and matching:
    sys.stdout.write(matching[0]["secret"])
elif action == "clear" and matching:
    items = [item for item in items if item not in matching]
else:
    code = 1
with open(os.environ["FAKE_KEYRING"], "w") as f:
    json.dump(items, f)
sys.exit(code)
"""


class FakeKeyring:
    def __init__(self, tmp_path):
        self.file = tmp_path / "keyring.json"
        self.log_file = tmp_path / "secret-tool-calls.jsonl"
        self.file.write_text("[]")

    def store(self, **fields):
        items = json.loads(self.file.read_text())
        for field, secret in fields.items():
            attributes = {"service": "um-vpn", "field": field}
            items.append({"attributes": attributes, "secret": secret})
        self.file.write_text(json.dumps(items))

    @property
    def fields(self):
        """What is stored for um-vpn, by field."""
        items = json.loads(self.file.read_text())
        return {
            item["attributes"]["field"]: item["secret"]
            for item in items
            if item["attributes"].get("service") == "um-vpn"
        }

    @property
    def calls(self):
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text().splitlines()
        return [json.loads(line) for line in lines]


@pytest.fixture(autouse=True)
def keyring(tmp_path, monkeypatch):
    bin_dir = tmp_path / "keyring-bin"
    bin_dir.mkdir()
    tool = bin_dir / "secret-tool"
    tool.write_text(f"#!{sys.executable}\n{FAKE_SECRET_TOOL}")
    tool.chmod(0o755)
    fake = FakeKeyring(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_KEYRING", str(fake.file))
    monkeypatch.setenv("FAKE_SECRET_TOOL_LOG", str(fake.log_file))
    return fake


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
    browser.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >{tmp_path}/argv\n'
        f"env >{tmp_path}/env\n"
    )
    browser.chmod(0o755)
    with pytest.raises(um_vpn.Error):
        um_vpn.browser_login(
            str(browser), tmp_path / "p", "https://x/", 5, ("id", "canary")
        )
    argv = (tmp_path / "argv").read_text().splitlines()
    assert f"--user-data-dir={tmp_path / 'p'}" in argv
    assert "--remote-debugging-pipe" in argv
    # The tunnel goes direct, so the login must too, or a desktop proxy
    # would sign in from a different address than the tunnel connects from.
    assert "--no-proxy-server" in argv
    assert argv[-1] == "https://x/"
    # ps shows every argv to every user, and children inherit the
    # environment; the password reaches Chrome through the pipe only.
    assert "canary" not in (tmp_path / "argv").read_text()
    assert "canary" not in (tmp_path / "env").read_text()


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


# --- sign-in ----------------------------------------------------------------

# The parts of UM's ADFS page that um-vpn relies on, down to the handler
# behind the button, which turns a bare ID into ID@um.edu.mo.
SIGN_IN_PAGE = """\
<!doctype html>
<meta charset="utf-8">
<title>Sign In</title>
<form method="post" id="loginForm" action="/adfs/ls/">
<span id="errorText" role="alert">ERROR</span>
<input id="userNameInput" name="UserName" type="email">
<input id="passwordInput" name="Password" type="password">
<span id="submitButton" role="button"
    onclick="return Login.submitLoginRequest();">Sign in</span>
</form>
<script>
var Login = {
    submitLoginRequest: function () {
        var name = document.getElementById("userNameInput");
        if (name.value.indexOf("@") < 0) {
            name.value += "@um.edu.mo";
        }
        document.forms["loginForm"].submit();
        return false;
    }
};
</script>
"""


class UmpassHandler(http.server.BaseHTTPRequestHandler):
    """The portal and UMPASS's ADFS, told apart by the Host header.

    The portal sends a visitor to ADFS until the sign-in succeeds, then
    sets DSID. ADFS takes one ID and password, and answers anything else
    with its form again, the error text filled in.
    """

    def do_GET(self):
        server = self.server
        if self.headers["Host"].partition(":")[0] != server.portal_host:
            server.form_served.set()
            self.send(SIGN_IN_PAGE.replace("ERROR", server.error_on_load))
        elif server.signed_in:
            cookie = f"DSID={server.dsid}; Path=/; HttpOnly"
            self.send("<!doctype html><title>portal</title>", cookie)
        else:
            self.redirect(server.sign_in_host, "/adfs/ls/")

    def do_POST(self):
        server = self.server
        body = self.rfile.read(int(self.headers["Content-Length"]))
        fields = urllib.parse.parse_qs(body.decode())
        server.posts.append({name: value[0] for name, value in fields.items()})
        if server.posts[-1] == server.account:
            server.signed_in = True
            self.redirect(server.portal_host, "/")
        else:
            error = "Incorrect user ID or password."
            self.send(SIGN_IN_PAGE.replace("ERROR", error))

    def send(self, page, cookie=None):
        body = page.encode()
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, host, path):
        self.send_response(302)
        port = self.server.server_port
        self.send_header("Location", f"http://{host}:{port}{path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def umpass():
    """The portal at sslvpn.um.localhost, its ADFS at websso1.um.localhost.

    Chrome takes every *.localhost name to 127.0.0.1 by itself.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UmpassHandler)
    server.portal_host = "sslvpn.um.localhost"
    server.sign_in_host = "websso1.um.localhost"
    # What a form or a shell could mangle, and a letter beyond ASCII.
    server.password = f"p a$s'\"\\wörd {secrets.token_hex(4)}"
    server.account = {
        "UserName": "s1234567@um.edu.mo",
        "Password": server.password,
    }
    server.dsid = secrets.token_hex(16)
    server.signed_in = False
    server.error_on_load = ""
    server.posts = []
    server.form_served = threading.Event()
    server.url = f"http://{server.portal_host}:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_sign_in_fills_the_form_once_and_returns_the_dsid(
    umpass, headless_chrome, tmp_path, keyring, capsys
):
    sign_in = ("s1234567", umpass.password)
    dsid = um_vpn.browser_login(
        headless_chrome, tmp_path / "profile", umpass.url, 30, sign_in
    )
    assert dsid == umpass.dsid
    # One post, with the ID made whole by the page's own handler.
    assert umpass.posts == [umpass.account]
    assert keyring.calls == []  # nothing forgotten
    assert "Filled in" in capsys.readouterr().err
    assert chrome_left_running(tmp_path) == []


def test_a_refused_password_is_sent_once_and_forgotten(
    umpass, headless_chrome, tmp_path, keyring, capsys
):
    keyring.store(username="s1234567", password="wrong")
    with pytest.raises(um_vpn.Error, match="no session"):
        um_vpn.browser_login(
            headless_chrome,
            tmp_path / "profile",
            umpass.url,
            8,
            ("s1234567", "wrong"),
        )
    # UMPASS locks the account after a few wrong passwords.
    assert len(umpass.posts) == 1
    assert keyring.fields == {}
    assert "forgotten" in capsys.readouterr().err


def test_a_form_that_already_shows_an_error_is_left_alone(
    umpass, headless_chrome, tmp_path, keyring, capsys
):
    umpass.error_on_load = "Your account is locked."
    keyring.store(username="s1234567", password=umpass.password)
    with pytest.raises(um_vpn.Error, match="no session"):
        um_vpn.browser_login(
            headless_chrome,
            tmp_path / "profile",
            umpass.url,
            5,
            ("s1234567", umpass.password),
        )
    assert umpass.form_served.is_set()
    assert umpass.posts == []
    assert keyring.fields  # never tried, so kept
    assert "already shows an error" in capsys.readouterr().err


def test_the_password_never_reaches_a_host_outside_the_login_domain(
    umpass, headless_chrome, tmp_path
):
    umpass.sign_in_host = "websso1.elsewhere.localhost"
    with pytest.raises(um_vpn.Error, match="no session"):
        um_vpn.browser_login(
            headless_chrome,
            tmp_path / "profile",
            umpass.url,
            5,
            ("s1234567", umpass.password),
        )
    # Otherwise a form that never showed up would pass as well.
    assert umpass.form_served.is_set()
    assert umpass.posts == []


def test_the_page_itself_turns_away_a_host_it_is_not_on(
    umpass, headless_chrome, tmp_path
):
    # The last check, inside the page: it holds even if um-vpn has picked
    # the wrong tab, or the tab has moved on since.
    chrome, devtools = um_vpn.launch_chrome(
        headless_chrome, tmp_path / "profile", umpass.url
    )

    def ask(scheme, host, password=None):
        targets = devtools.call("Target.getTargets")["targetInfos"]
        tabs = [t["targetId"] for t in targets if t["type"] == "page"]
        if not tabs:
            return None  # not open yet
        user = "s1234567" if password else None
        try:
            answer = um_vpn.call_in_page(
                devtools,
                tabs[0],
                um_vpn.FILL_SIGN_IN,
                scheme,
                host,
                user,
                password,
            )
        except um_vpn.Error:
            return None  # between pages
        return answer and answer[0]

    try:
        deadline = time.monotonic() + 20
        while ask("http:", umpass.sign_in_host) != "form":
            assert time.monotonic() < deadline
            time.sleep(0.2)
        elsewhere = "websso1.elsewhere.localhost"
        assert ask("http:", elsewhere, umpass.password) == "elsewhere"
        assert ask("https:", umpass.sign_in_host, umpass.password) == (
            "elsewhere"
        )
    finally:
        um_vpn.close_chrome(chrome, devtools)
    assert umpass.posts == []


@pytest.mark.parametrize(
    "host, domain",
    [
        ("sslvpn.um.edu.mo", "um.edu.mo"),
        ("sslvpn.um.localhost", "um.localhost"),
        # One label would take in every site under a top-level domain.
        ("vpn.example", None),
        ("localhost", None),
        ("127.0.0.1", None),
        ("::1", None),
    ],
)
def test_the_login_domain_is_the_portal_s_host_minus_one_label(host, domain):
    assert um_vpn.login_domain(host) == domain


class FakeTabs:
    """Answers Target.getTargets from a list that a test can change."""

    def __init__(self, *urls):
        self.targets = [
            {"targetId": str(i), "type": "page", "url": url}
            for i, url in enumerate(urls)
        ]

    def call(self, method, **params):
        assert method == "Target.getTargets"
        return {"targetInfos": self.targets}


def test_the_sign_in_looks_only_at_tabs_in_the_login_domain():
    tabs = FakeTabs(
        "https://websso1.um.edu.mo/adfs/ls/",
        "http://websso1.um.edu.mo/adfs/ls/",
        "https://evil-um.edu.mo/",
        "https://websso1.um.edu.mo.evil.com/",
        "https://api-1.duosecurity.com/",
        "chrome://newtab/",
    )
    frame = {"targetId": "f", "type": "iframe", "url": "https://um.edu.mo/"}
    tabs.targets.append(frame)
    form = um_vpn.SignIn(tabs, "https://sslvpn.um.edu.mo", "s1234567", "pw")
    assert form.tabs() == {"0": "websso1.um.edu.mo"}


FAILED = um_vpn.Error("Cannot find context with specified id")
SAYS = {
    "accepted": None,
    "refused": "forgotten",
    "by hand": "sign in by hand",
    "left alone": "already shows an error",
}


@pytest.mark.parametrize(
    "answers, outcome",
    [
        # A clean form takes the password, and the next page decides.
        ([["form", 1], ["submitted", 1], ["other", 2]], "accepted"),
        ([["form", 1], ["submitted", 1], ["form", 2]], "refused"),
        ([["form", 1], ["submitted", 1], ["error", 2]], "refused"),
        # The page that sent the form only counts with an error in place.
        (
            [["form", 1], ["submitted", 1], ["other", 1], ["error", 1]],
            "refused",
        ),
        (
            [["form", 1], ["submitted", 1], ["loading", 2], ["other", 2]],
            "accepted",
        ),
        # A call that failed may have sent the form all the same.
        ([["form", 1], FAILED, ["form", 2]], "refused"),
        ([["form", 1], FAILED, ["other", 2]], "accepted"),
        # Whatever the one call with the password answers, it is the only
        # one.
        ([["form", 1], ["other", 1]], "by hand"),
        # Nothing goes into a form that already shows an error, or before a
        # clean form shows up.
        ([["error", 1]], "left alone"),
        (
            [
                FAILED,
                ["loading", 1],
                ["other", 1],
                ["form", 2],
                ["submitted", 2],
                ["other", 3],
            ],
            "accepted",
        ),
    ],
)
def test_the_password_goes_out_once_and_the_next_page_decides(
    monkeypatch, keyring, capsys, answers, outcome
):
    keyring.store(username="s1234567", password="pw")
    sent = []

    def page(devtools, tab, function, scheme, host, user, password):
        sent.append(password)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(um_vpn, "call_in_page", page)
    tabs = FakeTabs("https://websso1.um.edu.mo/adfs/ls/")
    form = um_vpn.SignIn(tabs, "https://sslvpn.um.edu.mo", "s1234567", "pw")
    while answers:
        assert not form.done
        form.step()
    assert form.done
    assert set(sent) <= {None, "pw"}
    assert sent.count("pw") == (0 if outcome == "left alone" else 1)
    assert (keyring.fields == {}) == (outcome == "refused")
    if SAYS[outcome]:
        assert SAYS[outcome] in capsys.readouterr().err


def test_a_tab_that_leaves_for_duo_is_past_the_password(monkeypatch, keyring):
    keyring.store(username="s1234567", password="pw")
    answers = [["form", 1], ["submitted", 1]]
    monkeypatch.setattr(um_vpn, "call_in_page", lambda *a: answers.pop(0))
    tabs = FakeTabs("https://websso1.um.edu.mo/adfs/ls/")
    form = um_vpn.SignIn(tabs, "https://sslvpn.um.edu.mo", "s1234567", "pw")
    form.step()
    tabs.targets[0]["url"] = "https://api-1.duosecurity.com/frame/v4/auth"
    form.step()
    assert form.done
    assert answers == []
    assert keyring.fields  # kept


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
    """Stands in for the browser login; records each (url, sign_in)."""
    calls = []

    def fake_login(browser, profile, url, timeout=None, sign_in=None):
        calls.append((url, sign_in))
        return "fake-dsid"

    monkeypatch.setattr(um_vpn, "browser_login", fake_login)
    return calls


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
    assert login == [("https://sslvpn.um.edu.mo", None)]
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


def test_on_signs_in_with_the_stored_id_and_password(nm, login, keyring):
    keyring.store(username="s1234567", password="stored-password")
    assert um_vpn.main(["on"]) == 0
    assert login == [
        ("https://sslvpn.um.edu.mo", ("s1234567", "stored-password"))
    ]
    argvs = [" ".join(call["argv"]) for call in nm.calls + keyring.calls]
    assert not any("stored-password" in argv for argv in argvs)


def test_on_without_a_stored_sign_in_asks_the_keyring_once(nm, login, keyring):
    assert um_vpn.main(["on"]) == 0
    assert login == [("https://sslvpn.um.edu.mo", None)]
    # Each lookup can raise the keyring's unlock prompt.
    assert [call["argv"] for call in keyring.calls] == [
        ["lookup", "service", "um-vpn", "field", "username"]
    ]


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


def test_bare_um_vpn_shows_help_and_touches_nothing(
    nm, login, keyring, capsys
):
    nm.set(exists=True, active=True)
    assert um_vpn.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: um-vpn")
    for command in ("on", "off", "status", "remember", "forget"):
        assert command in out
    assert "keepalive" not in out  # only 'on' has a use for it
    # Running it to see what it does must not cost a Duo push or the tunnel.
    assert nm.calls == []
    assert login == []
    assert keyring.calls == []


class Terminal:
    """A stdin that is a terminal; nothing reads from it."""

    def isatty(self):
        return True


def test_remember_stores_the_sign_in_through_stdin(monkeypatch, keyring):
    keyring.store(username="old-id", password="old-password")
    password = "p a$s'\"\\wörd "  # kept exactly, trailing space and all
    monkeypatch.setattr(um_vpn.sys, "stdin", Terminal())
    monkeypatch.setattr("builtins.input", lambda prompt: " s1234567 ")
    monkeypatch.setattr(um_vpn.getpass, "getpass", lambda prompt: password)
    assert um_vpn.main(["remember"]) == 0
    assert keyring.fields == {"username": "s1234567", "password": password}
    # Cleared first, so that a store failing halfway cannot pair the new ID
    # with the old password.
    actions = [call["argv"][0] for call in keyring.calls]
    assert actions == ["clear", "store", "store"]
    # ps shows every argv to every user.
    argvs = [" ".join(call["argv"]) for call in keyring.calls]
    assert not any("wörd" in argv for argv in argvs)


def test_remember_needs_secret_tool(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert um_vpn.main(["remember"]) == 1
    assert "install libsecret-tools" in capsys.readouterr().err


def test_remember_needs_a_terminal(monkeypatch, keyring, capsys):
    monkeypatch.setattr(um_vpn.sys, "stdin", io.StringIO())
    assert um_vpn.main(["remember"]) == 1
    assert "terminal" in capsys.readouterr().err
    assert keyring.calls == []


def test_off_when_disconnected_changes_nothing(nm, capsys):
    assert um_vpn.main(["off"]) == 0
    assert nm.called("down") == []
    assert "Not connected" in capsys.readouterr().err


def test_forget_removes_the_connection_the_profile_and_the_sign_in(
    nm, keyring, capsys
):
    nm.set(exists=True, active=True)
    profile = um_vpn.profile_dir()
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("")
    keyring.store(username="s1234567", password="stored-password")
    assert um_vpn.main(["forget"]) == 0
    assert not nm.state["exists"]
    assert not profile.exists()
    assert keyring.fields == {}
    assert "the stored UMPASS sign-in" in capsys.readouterr().err
    assert um_vpn.main(["forget"]) == 0  # and again, with nothing left
    assert "Nothing to do" in capsys.readouterr().err


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
