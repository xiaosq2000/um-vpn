# Design decisions

Why um-vpn is built the way it is, and what was tried instead. Newest first.

## 2026-09: one Python file on NetworkManager

The first version was a bash script that ran `sudo openconnect` itself, plus a
Python helper that read the cookie over a DevTools port. Much of its code, and
most of its instructions for AI agents, dealt with problems that design created.
The rewrite removes those problems instead of documenting them.

- **NetworkManager runs the tunnel, not `sudo openconnect`.** Its polkit rules
  let the desktop user connect without a password. It owns the openconnect
  process, so there is no root-owned PID file in the home directory, no `/proc`
  probing and no teardown script. It sets routes and DNS, and it puts the tunnel
  in GNOME's quick settings. Tried on 2026-09-24 with NetworkManager 1.54 and
  network-manager-openconnect 1.2.10: the tunnel was up in about a second. UM
  pushes 7 routes and leaves the default route alone, so it is a split tunnel.
  - nmcli takes the cookie from a `passwd-file`, which um-vpn feeds through a
    pipe. nmcli gives up on the whole request if any secret it asks for is
    missing, so `gwcert` and `resolve` are passed empty. Otherwise GNOME opens
    NetworkManager's own login dialog, which cannot do UM's login.
  - nmcli reports a refused cookie only as "Unknown reason", so um-vpn quotes
    openconnect's own lines from the journal.
- **No cookie cache.** Disconnecting makes openconnect log the session out, and
  NetworkManager disconnects on suspend too, so a saved cookie would almost
  never work again. Every connect signs in afresh; ADFS's "keep me signed in"
  and Duo's "remember this device" make that a window that closes by itself.
  Nothing secret is stored on disk.
- **No password autofill.** The bash version typed the UMPASS password from the
  desktop keyring into the ADFS form. That was the riskiest code in the project,
  and it only helped once the ADFS sign-in had expired. Chrome's own password
  manager, in the login profile, fills the form and leaves one click.
- **DevTools over a pipe, not a port.** With `--remote-debugging-pipe`, only
  um-vpn can talk to Chrome. The port it replaces was open to every local
  process while the login window was up, and it needed a hand-written WebSocket
  client.
- **The login goes direct.** Chrome starts with `--no-proxy-server`, because the
  tunnel connects to the gateway directly and ignores desktop proxies. Before
  this, users behind a proxy had to switch it off around each connect.
- **One Python file, standard library only.** Python 3 is on every Ubuntu
  desktop, so the file installs by being copied. A Go binary would be as easy to
  hand out, but it would need a full port and a build step.
- **Tests instead of warnings.** Headless Chrome against a stand-in portal
  checks the login. A fake nmcli checks what reaches NetworkManager, and that
  the cookie never lands on a command line.

## 2026-08: credentials in the keyring (removed 2026-09)

The bash version could store the UMPASS ID and password in the login keyring and
type them into the ADFS form over DevTools, at most once per login, because
UMPASS locks the account after a few bad passwords. It was chosen over Chrome's
password manager, which fills the form but does not submit it, and over a `gpg`
file or `pass`, which only trade one unlock prompt for another.

## 2026-07, re-checked 2026-08: how to authenticate at all

`https://sslvpn.um.edu.mo` redirects straight to ADFS over SAML, with Duo as an
ADFS MFA adapter.

- **Official Ivanti client:** no package for Ubuntu 26.04.
- **`openconnect --protocol=nc` with a username and password:** impossible. `nc`
  cannot follow the SAML redirect, and `--external-browser` only works for
  `anyconnect` and `gp`.
- **`openconnect --protocol=pulse`, OpenConnect 9.12 from apt:** rejected in the
  handshake with
  `errorType=6 errorString=clientCapabilities key or value not present`. Gateway
  firmware updated after CVE-2025-0282 requires an attribute that 9.12 does not
  send.
- **`--protocol=pulse` from OpenConnect's git master:** passes the handshake,
  then fails with `Authentication failure: Code 0x00`, whatever `--useragent` or
  `--os` claim. Most likely a server-side realm policy, such as a minimum client
  version or Host Checker.
- **`--protocol=pulse --cookie <DSID>`:** rejected the same way.
- **Reading `DSID` from Chrome's cookie database:** unreliable. It is an
  HttpOnly session cookie that may never be written to disk, and what is written
  is encrypted with a key from the keyring.
- **NetworkManager's openconnect plugin with its own login dialog:** its
  embedded WebKit window was an unknown against Duo's Universal Prompt. In
  2026-09 NetworkManager became the tunnel instead, with um-vpn doing the login.
- **`--protocol=nc` with a `DSID` taken from a browser login:** works, even with
  stock OpenConnect 9.12, because the legacy protocol skips the newer client
  policy checks. Everything in um-vpn follows from this.
