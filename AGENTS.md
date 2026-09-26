# AGENTS.md

um-vpn connects to the University of Macau SSL VPN. It reads the portal's `DSID`
session cookie from a Chrome login over DevTools and hands it to
NetworkManager's openconnect plugin. All of it is in `um_vpn.py`. README.md is
for users; docs/decisions.md records why it is built this way and what failed
before, and docs/debugging.md shows how to find out why a tunnel failed.

## Verify

`uv run pytest && pre-commit run --all-files` must pass before a commit. The
tests drive headless Chrome against a stand-in portal and ADFS on 127.0.0.1,
with a fake nmcli and a fake secret-tool: no Duo, no network change, no sudo, no
real keyring.

## Hard limits

`.claude/settings.json` enforces what it can of these.

- Don't run `um-vpn` bare, `um-vpn on|off|remember|forget`, or
  `nmcli connection up|down|delete`. Each costs the user a real Duo push, or
  changes their real network or their stored sign-in. `um-vpn status` is safe;
  ask the user to run the rest.
- Don't read `~/.local/share/um-vpn/`, whose Chrome profile holds Duo's
  remembered device, and don't run `secret-tool`, which reaches the UMPASS
  password in the keyring. Never print a `DSID` value.
- Never put a secret in argv or the environment. `ps` shows every argv to every
  user, and children inherit the environment.
- Leave proxy and VPN clients on the machine alone. um-vpn works alongside them,
  and your own connection may run through one.

## What you learn

Put it in the repo, where agents on every machine will find it, and not in an
agent's private memory. Remove a trap or pin it with a test, record a design
decision or a failed attempt in docs/decisions.md, and add a way to diagnose a
failure to docs/debugging.md. Don't add a paragraph here.

## Conventions

Conventional Commits. PEP 8 at 79 columns, enforced by ruff. Comments say why,
not what. The repository will become public, so commits carry no
machine-specific paths or personal details.
