# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A toggle for the University of Macau SSL VPN. `bin/um-vpn` (bash) drives `openconnect --protocol=nc`; `libexec/um-vpn/get-dsid.py` (python3) opens Chrome on the portal, waits for UMPASS + Duo, and lifts the HttpOnly `DSID` session cookie out of the live browser over the DevTools protocol. `um-sslvpn-openconnect.md` is the README in substance.

`~/.local/bin/um-vpn` is a symlink into this working tree, so edits to `bin/um-vpn` change the installed command immediately.

## Verifying changes

There is no build and no test suite. `pre-commit run --all-files` is the entire harness — see `.pre-commit-config.yaml` for what it covers. It is clean on `main`, so any output is something you introduced.

`um-vpn status` is the only safe runtime check. Do not run `um-vpn on|off|toggle|renew` to test a change — each connect costs a real Duo push and drops or raises the user's actual network. Ask first.

## Load-bearing details

Breaking any of these fails in a way the diff does not show.

- **`--help` is the header comment.** `usage()` prints `sed -n '2,12p' "$SELF"`. Inserting or deleting lines anywhere in `bin/um-vpn` lines 2–12 silently corrupts the help output; adding a subcommand means editing that block too.
- **`bin/` → `libexec/` is a relative lookup** via `readlink -f "$0"` + `../libexec/um-vpn/get-dsid.py`. A symlink to `bin/um-vpn` is fine; moving one file without the other is not.
- **Helper contract:** `get-dsid.py` prints the cookie value on **stdout**, everything human-facing on **stderr**, and exits 1 on failure / 130 on Ctrl-C. `fresh_dsid()` captures stdout, so any stray `print()` to stdout becomes part of the cookie.
- **`tunnel_pid()` probes `/proc`, not `kill -0`** — openconnect runs as root, so `kill -0` returns EPERM. Do not "simplify" it.
- **Teardown is a single `sudo sh -c`** (TERM → 10s poll → KILL → rm pidfile) so it costs at most one password prompt, and `require_sudo` runs `sudo -v` *before* Chrome launches so the prompt never hides behind the login window.
- **A rejected cached cookie is the normal expiry path**, not an error: `cmd_on` tries the cached DSID first and only falls back to a browser login when openconnect refuses it.
- **Chrome specifics in `get-dsid.py`:** `--remote-debugging-port=0` with the real port read from `DevToolsActivePort`; an already-open window on the profile is reused (Chrome would otherwise hand the launch off and exit); shutdown goes through `Browser.close` so the ADFS "keep me signed in" cookie is flushed to the profile.

## Secrets

The DSID **is** a live authenticated session — treat it as a password.

- Pass it via `--cookie-on-stdin`, never `--cookie` (which exposes it in `ps`).
- The state dir is `install -d -m 700`; the cookie is written under `(umask 077)`.
- Never echo a DSID value, a log line containing one, or the contents of the state dir into the transcript.
- All state lives outside the repo under XDG dirs. Nothing secret should land in the working tree.

## Style

`.editorconfig` and `ruff.toml` hold the mechanical rules — Google Shell Style (2-space, 80 cols) for `bin/um-vpn`, PEP 8 for the Python — and pre-commit enforces them. What they can't encode:

- `set -euo pipefail`; `die`/`note` for every user-facing message, both to stderr; `# --- section ---` banners.
- Comment *why* a non-obvious choice was made, not what a line does. Both files do this consistently; keep it.
- Configuration constants live in the block at the top of `bin/um-vpn` (`SERVER`, `DOMAIN`, `VPNC_SCRIPT`, `BROWSER`) — add new ones there rather than inlining.
- Commits follow Conventional Commits (`feat:`, `fix:`). No remote is configured yet.

## Dependencies

`get-dsid.py` is standard-library-only today — Ubuntu is PEP-668 externally-managed, and the ~90-line WebSocket client exists specifically to avoid a dependency. If a dependency ever becomes genuinely necessary, reach for `uv` or `pixi`; never `pip install` into the system Python.

## Direction

This is headed toward being shareable with other UM users and needs packaging (README, license, install script). Prefer env-overridable or flag-driven configuration over new hardcoded machine-specific paths.

## Docs

`um-sslvpn-openconnect.md` is the user-facing doc and carries a dated "Why not the other methods" decision log. Update it in the same pass as behaviour changes, and keep its style: tables, and an explicit record of *why* each alternative was rejected.
