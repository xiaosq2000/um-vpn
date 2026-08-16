# UM SSL VPN on Ubuntu with OpenConnect

_Written 2026-07-02; reworked 2026-08-16 into a one-word toggle. Verified
against Ubuntu 26.04 (OpenConnect 9.12, Chrome 151) and the UM gateway at that
date. If ICTO upgrades the gateway firmware or changes the ADFS login flow,
revisit Troubleshooting._

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
`libexec/um-vpn/get-dsid.py` is standard library only (Ubuntu 26.04 is PEP-668
managed and this was not worth a virtualenv).

### Development

There is no build and no test suite, so [pre-commit](https://pre-commit.com) is
the whole check:

```bash
pre-commit install          # once per clone, installs the git hook
pre-commit run --all-files  # on demand
```

It runs shellcheck, `ruff check` and `ruff format`, the `.editorconfig` rules,
and a guard that refuses to commit anything containing a DSID value.
`bin/um-vpn` follows the Google Shell Style Guide (2-space indent, 80 columns);
`get-dsid.py` follows PEP 8. `.editorconfig` and `ruff.toml` are the source of
truth for both.

## Usage

| Command         | Effect                                                            |
| --------------- | ----------------------------------------------------------------- |
| `um-vpn`        | Toggle — connect if down, disconnect if up                        |
| `um-vpn on`     | Connect, reusing the cached session cookie when it still works    |
| `um-vpn off`    | Disconnect                                                        |
| `um-vpn status` | Tunnel state, interface address, cookie age, log path             |
| `um-vpn renew`  | Discard the cached cookie, force a fresh browser login, reconnect |

A connect asks for your sudo password up front (before any window appears), then
either reuses the cached cookie or opens a login window.

## How the cookie is obtained

`DSID` is an **HttpOnly session cookie**. It lives in Chrome's memory and never
reliably reaches the on-disk cookie database, and what does reach disk is
AES-encrypted. Scraping `Cookies` files is therefore both fragile and encrypted
for nothing.

Instead, `get-dsid.py`:

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

## Files and state

| Path                                    | Contents                                |
| --------------------------------------- | --------------------------------------- |
| `bin/um-vpn`                            | The toggle                              |
| `libexec/um-vpn/get-dsid.py`            | Browser login + DevTools cookie harvest |
| `~/.local/state/um-vpn/dsid`            | Cached DSID, mode 600                   |
| `~/.local/state/um-vpn/openconnect.pid` | Tunnel PID (written by root)            |
| `~/.local/state/um-vpn/openconnect.log` | Timestamped openconnect output          |
| `~/.local/share/um-vpn/chrome`          | Dedicated Chrome profile, mode 700      |

## Security notes

- The cookie is passed with `--cookie-on-stdin`, not `--cookie`. With `--cookie`
  the DSID is visible in `ps` output to every user on the machine, and lands in
  shell history. Treat the DSID like a password: it _is_ your authenticated
  session.
- `~/.local/state/um-vpn/dsid` is mode 600. It is a live session until the
  gateway expires it; `um-vpn renew` invalidates your local copy.
- The dedicated Chrome profile holds an ADFS SSO cookie once you tick "keep me
  signed in". That is the price of silent reconnects. Delete the profile
  directory to revoke it.
- Do **not** click Logout on the portal — that invalidates the cookie
  server-side.
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
