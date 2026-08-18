# UM SSL VPN on Ubuntu with OpenConnect

Verified against Ubuntu 26.04 (OpenConnect 9.12, Chrome 151) and the UM gateway
at that date. If ICTO upgrades the gateway firmware or changes the ADFS login
flow, revisit Troubleshooting.

The official **Ivanti Secure Access Client** does not support recent Ubuntu
releases. This is the working alternative: OpenConnect's legacy Network Connect
(`nc`) protocol, authenticated with a `DSID` session cookie taken from a real
browser login.

Everything below exists to make that one cookie cheap to obtain, so day-to-day
use is just:

```bash
um-vpn          # connect if down, disconnect if up
```

## Why a browser is unavoidable

`https://sslvpn.um.edu.mo` immediately redirects to
`https://websso1.um.edu.mo/adfs/ls/?SAMLRequest=…` — UM federates the VPN login
to **ADFS via SAML**, with Duo riding along as an ADFS MFA adapter.

OpenConnect's `nc` protocol has no SAML support, and `--external-browser` only
applies to protocols with SSO handling (`anyconnect`, `gp`). There is therefore
no invocation of openconnect that can authenticate on its own — a browser must
complete UMPASS + Duo and hand over the resulting `DSID`.

So the cookie method is not a workaround for a missing feature. It is the only
door, and the work is in making it painless.

## Install

```bash
sudo apt install openconnect          # stock package is fine; no source build
```

The scripts live in this repository and are linked onto `PATH`:

```bash
ln -s "$PWD/bin/um-vpn" ~/.local/bin/um-vpn
```

`bin/um-vpn` finds its helper relative to its own resolved path, so the symlink
is enough — but the two files must move together. No Python packages are needed;
`libexec/um-vpn/browser-login.py` is standard library only (Ubuntu 26.04 is
PEP-668 managed and this was not worth a virtualenv).

### Uninstall

```bash
um-vpn uninstall
```

It lists what it is about to delete, waits for you to type `uninstall`, then
disconnects the tunnel if it is up, removes every `um-vpn` symlink on `PATH`
that resolves to this clone, deletes `~/.local/state/um-vpn` and
`~/.local/share/um-vpn`, and clears any saved UMPASS credentials from the
keyring.

Order matters, which is the reason this is a subcommand rather than a line in
this file: deleting the state directory while connected takes the PID file with
it, leaving `openconnect` running as root with your routes and no supported way
to stop it.

Deliberately left behind:

| Left in place                 | Why                                                                               |
| ----------------------------- | --------------------------------------------------------------------------------- |
| The clone                     | It _is_ the program. Delete it yourself; reinstalling is the same one `ln -s`     |
| `openconnect`, `vpnc-scripts` | System packages another VPN may need — `sudo apt remove openconnect vpnc-scripts` |
| Your portal session           | Deleting the cookie revokes only this machine's copy — see below                  |

The gateway keeps your session alive until it expires on its own, so uninstall
inverts the usual advice: this is the one time you _should_ open the portal and
click Logout, to kill it server-side. The ADFS "keep me signed in" cookie and
Duo's "remember this device" lived in the deleted Chrome profile, so those are
gone with it.

### Configuration

The defaults target UM on Ubuntu, so nothing needs setting. Each is overridable
from the environment:

| Variable               | Default                                           | Purpose                                                                                                                                                 |
| ---------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UM_VPN_PORTAL_URL`    | `https://sslvpn.um.edu.mo`                        | Portal URL                                                                                                                                              |
| `UM_VPN_COOKIE_DOMAIN` | host part of `UM_VPN_PORTAL_URL`                  | Domain the cookie must belong to                                                                                                                        |
| `UM_VPN_BROWSER`       | first Chromium-family on `PATH`                   | Browser to drive for the login                                                                                                                          |
| `UM_VPN_VPNC_SCRIPT`   | first path found (below)                          | `vpnc-script` location                                                                                                                                  |
| `UM_VPN_LOGIN_DOMAIN`  | `UM_VPN_COOKIE_DOMAIN` without its leftmost label | Domain the saved credentials may be typed into. Two labels minimum — a single-label value is refused rather than opening every `https` page under a TLD |
| `UM_VPN_AUTOFILL`      | `on`                                              | `off` keeps the credentials but leaves the form to you                                                                                                  |

Because the cookie domain defaults to the portal URL’s host, pointing this at a
different Juniper/Pulse portal takes only `UM_VPN_PORTAL_URL`.

`vpnc-script` is looked for at `/usr/share/vpnc-scripts/vpnc-script`,
`/etc/vpnc/vpnc-script`, `/usr/share/vpnc/vpnc-script`,
`/usr/local/etc/vpnc/vpnc-script` and `/opt/homebrew/etc/vpnc/vpnc-script`, in
that order. On Debian and Ubuntu it ships in the `vpnc-scripts` package.

The browser must be Chromium-family — `google-chrome`, `google-chrome-stable`,
`chromium` or `chromium-browser`. Firefox cannot stand in: the login is driven
over the Chrome DevTools Protocol, which it does not implement.

### Development

There is no build and no test suite, so [pre-commit](https://pre-commit.com) is
the whole check:

```bash
pre-commit install          # once per clone, installs the git hook
pre-commit run --all-files  # on demand
```

It runs shellcheck, `ruff check` and `ruff format`, prettier over the Markdown,
the `.editorconfig` rules, and a guard that refuses to commit anything
containing a DSID value. `bin/um-vpn` follows the Google Shell Style Guide
(2-space indent, 80 columns); `browser-login.py` follows PEP 8; the docs are
wrapped at 80 too. `.editorconfig`, `ruff.toml` and `.prettierrc.yaml` are the
source of truth.

## Usage

| Command              | Effect                                                            |
| -------------------- | ----------------------------------------------------------------- |
| `um-vpn`             | Toggle — connect if down, disconnect if up                        |
| `um-vpn on`          | Connect, reusing the cached session cookie when it still works    |
| `um-vpn off`         | Disconnect                                                        |
| `um-vpn status`      | Tunnel state, interface address, cookie age, log path             |
| `um-vpn renew`       | Discard the cached cookie, force a fresh browser login, reconnect |
| `um-vpn credentials` | Save the UMPASS ID + password so the sign-in form fills itself    |
| `um-vpn uninstall`   | Disconnect, then remove the command, the cookie and the profile   |

A connect asks for your sudo password up front (before any window appears), then
either reuses the cached cookie or opens a login window.

## How the cookie is obtained

`DSID` is an **HttpOnly session cookie**. It lives in Chrome's memory and never
reliably reaches the on-disk cookie database, and what does reach disk is
AES-encrypted. Scraping `Cookies` files is therefore both fragile and encrypted
for nothing.

Instead, `browser-login.py`:

1. Launches Chrome on a **dedicated profile** (`~/.local/share/um-vpn/chrome`)
   with `--remote-debugging-port=0`, and reads the port Chrome publishes in that
   profile's `DevToolsActivePort`.
2. Speaks the DevTools protocol over a WebSocket (~90 lines of stdlib, since
   `websockets` is not installed), polling `Storage.getCookies` once a second.
   This sees HttpOnly and session cookies live, with no decryption.
3. On seeing `DSID` for `sslvpn.um.edu.mo`, issues `Browser.close` — a
   _graceful_ shutdown, so the profile's ADFS state is flushed — and prints the
   value.

The window therefore closes by itself the moment you finish authenticating.

### Make subsequent logins near-silent

On the first login in the dedicated profile, tick **"Keep me signed in"** at the
ADFS prompt and **"Remember this device"** at the Duo prompt. After that, most
connects are a window that flashes open, redirects through ADFS, and closes — no
typing, no Duo push.

If the tunnel merely dropped and the cookie is still within its gateway
lifetime, there is no window at all: the cached cookie is tried first.

### Stop typing the UMPASS password

```bash
um-vpn credentials        # prompts for the ID, then the password
um-vpn credentials clear  # forget them again
```

Both land in the login keyring (libsecret, under `service=um-vpn`), which GNOME
unlocks with your desktop session. From then on `browser-login.py` fills the
sign-in form and submits it, so even a full login is one Duo tap and nothing
typed.

| Guard                                                                                                                                  | Why                                                                                                                                     |
| -------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| The password is typed only into `https://` pages under `UM_VPN_LOGIN_DOMAIN` — `um.edu.mo` by default, and never a single-label domain | A tab left open on anything else in the login profile can never be handed it                                                            |
| The form is filled once per login, never retried                                                                                       | UMPASS locks the account after a few bad passwords, and a retry loop would turn one typo into a lockout of everything, not just the VPN |
| The values reach the helper NUL-separated on stdin                                                                                     | `ps` shows argv to every user on the machine, and the environment would be inherited by Chrome                                          |

No credentials saved, keyring locked, `secret-tool` missing, or
`UM_VPN_AUTOFILL=off` — each falls back to the old behaviour: the window opens
and you type. This is a convenience, never a dependency.

## Files and state

| Path                               | Contents                                |
| ---------------------------------- | --------------------------------------- |
| `bin/um-vpn`                       | The toggle                              |
| `libexec/um-vpn/browser-login.py`  | Browser login + DevTools cookie harvest |
| `~/.local/state/um-vpn/cookie`     | Cached DSID, mode 600                   |
| `~/.local/state/um-vpn/tunnel.pid` | Tunnel PID (written by root)            |
| `~/.local/state/um-vpn/tunnel.log` | Timestamped openconnect output          |
| `~/.local/share/um-vpn/chrome`     | Dedicated Chrome profile, mode 700      |
| login keyring, `service=um-vpn`    | UMPASS ID and password, once saved      |

## Security notes

- The cookie is passed with `--cookie-on-stdin`, not `--cookie`. With `--cookie`
  the DSID is visible in `ps` output to every user on the machine, and lands in
  shell history. Treat the DSID like a password: it _is_ your authenticated
  session.
- `~/.local/state/um-vpn/cookie` is mode 600. It is a live session until the
  gateway expires it; `um-vpn renew` invalidates your local copy.
- The dedicated Chrome profile holds an ADFS SSO cookie once you tick "keep me
  signed in". That is the price of silent reconnects. Delete the profile
  directory — or run `um-vpn uninstall` — to revoke it.
- The UMPASS password, when saved, is in the login keyring rather than any file
  in this repo or under `~/.local`. It is handed to `browser-login.py` on stdin,
  and from there straight into a DevTools `Runtime.evaluate` on a page under
  `UM_VPN_LOGIN_DOMAIN` — never into argv, the environment, or a log line.
  `um-vpn credentials clear` removes it.
- The sign-in form is filled at most once per login. That is a lockout guard,
  not a UX choice: UMPASS is the account behind email and everything else.
- Do **not** click Logout on the portal — that invalidates the cookie
  server-side. The exception is uninstalling, where killing the session is the
  point.
- The tunnel runs with `--no-proxy`. This machine has
  `https_proxy=127.0.0.1:1080` in the environment; `sudo`'s `env_reset` already
  strips it, and `--no-proxy` pins that behaviour so an `env_keep` change cannot
  silently reroute the tunnel. If you ever need the VPN to dial _through_ a
  proxy, drop that flag.

## Why not the other methods (tried 2026-07, re-checked 2026-08)

| Method                                                                     | Result                                                                                                                                                                                                               |
| -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Official Ivanti client                                                     | No package for Ubuntu 26.04                                                                                                                                                                                          |
| `openconnect --protocol=nc` with username/password                         | Impossible: the portal 302s to ADFS SAML, which `nc` cannot speak, and `--external-browser` does not apply to this protocol                                                                                          |
| `openconnect --protocol=pulse` (apt 9.12)                                  | Rejected in handshake: `errorType=6 errorString=clientCapabilities key or value not present` — 9.12 predates the attribute, which post-CVE-2025-0282 gateway firmware requires                                       |
| `openconnect --protocol=pulse` (git master, sends `clientCapabilities={}`) | Passes the handshake but rejected pre-credentials with `Authentication failure: Code 0x00`, regardless of `--useragent` / `--os` spoofing — server-side realm policy (likely minimum client version or Host Checker) |
| `--protocol=pulse --cookie <DSID>`                                         | Rejected the same way                                                                                                                                                                                                |
| Reading `DSID` from Chrome's `Cookies` sqlite file                         | Unreliable: HttpOnly _session_ cookie, so it may never be written to disk, and on-disk values are AES-encrypted against a gnome-keyring key                                                                          |
| NetworkManager openconnect plugin (GNOME quick-settings toggle)            | Not pursued — plugin 1.2.10's embedded WebKit auth dialog is an unknown against Duo's Universal Prompt. Still a reasonable fallback if the terminal toggle ever stops fitting                                        |
| **`--protocol=nc --cookie-on-stdin` with a browser-harvested DSID**        | **Works**, even with stock 9.12 — the legacy protocol skips the modern client policy checks                                                                                                                          |

### Why the credentials live in the keyring (2026-08)

| Alternative                                                    | Result                                                                                                                                                                                              |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chrome's own password manager in the dedicated profile         | Rejected: it fills but does not submit, so the login stays hands-on, and the value sits in Chrome's `Login Data` where `um-vpn` can neither read it nor revoke it on uninstall                      |
| A file under `~/.local/share/um-vpn`, plain or `gpg`-encrypted | Rejected: plain text buys nothing the keyring does not already give; `gpg` just trades the UMPASS prompt for a pinentry one                                                                         |
| `pass` or an external password-manager command                 | Not pursued: another dependency and another unlock for the same result, when libsecret is already running and unlocked under GNOME                                                                  |
| Typing the form with `Input.dispatchKeyEvent`                  | Not pursued: per-keystroke round trips that depend on focus and keyboard layout. Setting the value through the native setter and firing `input`/`change` convinces script-driven pages just as well |
| **`secret-tool` plus a DevTools fill, submitted for you**      | **Works** — one `um-vpn credentials`, and logins cost a Duo tap                                                                                                                                     |

## Troubleshooting

- **`Cookie was rejected by server` (exit 2)** — expected when the cached DSID
  has expired; `um-vpn on` falls through to a browser login automatically. If it
  happens right after a fresh login, the portal was probably logged out in
  another window.
- **The login window opens but never closes** — authentication did not complete,
  or the DSID landed on an unexpected domain. Check the window; the helper gives
  up after 5 minutes. Closing the window yourself aborts cleanly.
- **`Chrome exited before the DevTools endpoint came up`** — usually another
  Chrome already owns the dedicated profile. The helper reuses a live window
  when it can; if it is wedged, delete
  `~/.local/share/um-vpn/chrome/SingletonLock`.
- **Tunnel state looks wrong** — `um-vpn status` reads `/proc` rather than
  `kill -0`, because the openconnect process is root-owned and `kill -0` returns
  EPERM rather than success. If the PID file is stale, `um-vpn off` clears it.
- **Slow throughput** — see the `--no-proxy` note above.
- **`nc` stops working after a gateway upgrade** — Ivanti may eventually disable
  the legacy protocol. Check
  <https://gitlab.com/openconnect/openconnect/-/issues> for the state of `pulse`
  protocol support; a source clone (not installed) lives at
  `~/.local/src/openconnect`, buildable with
  `./autogen.sh && ./configure --prefix=$HOME/.local --disable-shared --with-vpnc-script=/usr/share/vpnc-scripts/vpnc-script && make`.
- **Official Linux client returns** — check
  <https://faq.icto.um.edu.mo/how-to-setup-pulse-secure-client/> for an
  Ubuntu-compatible build; that would restore a browser-free login.

## TODO

- **Auto-reconnect on drop.** OpenConnect already retries for 300s by default
  (`--reconnect-timeout`), which covers a wifi hop. What it does not cover is
  suspend/resume or a cookie that expires mid-session — both need a supervisor
  that notices the tunnel died and re-runs `um-vpn on`. A systemd user unit with
  `Restart=on-failure`, or a `sleep.target` hook, is the obvious shape.
- **Desktop notifications.** `notify-send` on connected / dropped / cookie
  expired, so the tunnel's state is visible without running `um-vpn status`.
  Most valuable in combination with the supervisor above, since a silent
  background reconnect is exactly the case where you want to be told.
- **Split tunnel.** Route only UM subnets through the VPN via `vpn-slice`,
  instead of sending everything. Needs the UM subnet list, which can be read off
  the tunnel's routes on a first full-tunnel connect.

## License

MIT — see [LICENSE](LICENSE).
