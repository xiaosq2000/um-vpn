# AGENTS.md

um-vpn connects to the University of Macau SSL VPN. It reads the portal's `DSID`
session cookie from a Chrome login over DevTools and hands it to
NetworkManager's openconnect plugin. All of it is in `um_vpn.py`. README.md is
for users; docs/decisions.md records why it is built this way and what failed
before.

## Verify

`uv run pytest && pre-commit run --all-files` must pass before a commit. The
tests drive headless Chrome against a stand-in portal on 127.0.0.1 and a fake
nmcli: no Duo, no network change, no sudo.

## Hard limits

`.claude/settings.json` enforces what it can of these.

- Don't run `um-vpn` bare, `um-vpn on|off|forget`, or
  `nmcli connection up|down|delete`. Each costs the user a real Duo push or
  changes their real network. `um-vpn status` is safe; ask the user to run the
  rest.
- Don't read `~/.local/share/um-vpn/`: the Chrome profile there holds a live
  ADFS sign-in. Never print a `DSID` value.
- Never put a secret in argv or the environment. `ps` shows every argv to every
  user, and children inherit the environment.

## When you find a trap

Remove it, or pin it with a test. Don't add a paragraph here.

## Conventions

Conventional Commits. PEP 8 at 79 columns, enforced by ruff. Comments say why,
not what. The repository will become public, so commits carry no
machine-specific paths or personal details.
