# um-vpn

Connect to the University of Macau SSL VPN from a Linux desktop with one
command:

```bash
um-vpn          # connect if disconnected, disconnect if connected
```

The UM portal signs you in through ADFS and Duo, which no VPN client on Linux
can do by itself. um-vpn opens the portal in Chrome, waits while you sign in,
takes the session cookie from the browser, and hands it to NetworkManager, which
runs the tunnel with OpenConnect. No sudo.

> **Unofficial.** ICTO neither provides nor supports this. It relies on the
> gateway's legacy Network Connect protocol, which a firmware upgrade could
> switch off.

## Install

On Ubuntu, or any Linux desktop that uses NetworkManager:

```bash
sudo apt install network-manager-openconnect
curl -fLo ~/.local/bin/um-vpn https://raw.githubusercontent.com/xiaosq2000/um-vpn/main/um_vpn.py
chmod +x ~/.local/bin/um-vpn
```

You also need Google Chrome (the `.deb` from google.com) or a Chromium that is
not a snap, and Python 3.10 or later, which Ubuntu already has. With
[uv](https://docs.astral.sh/uv/),
`uv tool install git+https://github.com/xiaosq2000/um-vpn` works too.

## Use

| Command         | Effect                                                     |
| --------------- | ---------------------------------------------------------- |
| `um-vpn`        | Connect if disconnected, disconnect if connected           |
| `um-vpn on`     | Sign in through Chrome and connect                         |
| `um-vpn off`    | Disconnect, which also ends your portal session            |
| `um-vpn status` | Show whether the tunnel is up, and its address             |
| `um-vpn forget` | Delete the NetworkManager connection and the login profile |

GNOME's quick settings show the tunnel under VPN, and can disconnect it.

The first time, a Chrome window opens on the portal. Sign in with UMPASS, then:

- tick **Keep me signed in** on the ADFS page, and
- tick **Remember this device** at the Duo prompt.

Until those expire, a connect is a window that opens and closes by itself, with
nothing to type and no Duo push. If you also let Chrome save your UMPASS
password, an expired sign-in costs one click and a Duo tap.

## Configuration

Nothing needs setting for UM. Two environment variables change the defaults:

| Variable            | Default                          | Purpose                          |
| ------------------- | -------------------------------- | -------------------------------- |
| `UM_VPN_PORTAL_URL` | `https://sslvpn.um.edu.mo`       | Use another Pulse/Ivanti portal  |
| `UM_VPN_BROWSER`    | the first Chrome found on `PATH` | Sign in with a different browser |

## Troubleshooting

- **"Chrome is already running on um-vpn's profile"**: a login window is still
  open. Close it and run `um-vpn` again.
- **"NetworkManager could not connect"**: um-vpn prints OpenConnect's own
  explanation below the error. "Cookie was rejected by server" right after a
  login usually means the session was logged out in another window; run `um-vpn`
  again.
- **A "VPN authentication" dialog from GNOME**: that is NetworkManager's own
  login, which cannot sign in to UM. Cancel it and use `um-vpn`.
- **"…is a snap"**: snap confinement keeps Chromium out of the login profile in
  `~/.local/share`. Install Google Chrome's `.deb`.
- **Everything fails after a gateway upgrade**: ICTO may have switched off the
  legacy protocol. [docs/decisions.md](docs/decisions.md) lists what else has
  been tried.

## Limitations

- Chrome or Chromium only: the login is read over the Chrome DevTools Protocol,
  which Firefox does not speak.
- It needs a desktop session. Over SSH there is no window to sign in with, and
  the error only says the login window closed.
- NetworkManager disconnects VPNs on suspend. Run `um-vpn` again after resume.

## Security

- The `DSID` cookie is your live portal session. It goes from Chrome to
  NetworkManager through pipes; it is never written to disk or put on a command
  line, where `ps` would show it to every user on the machine.
- Chrome's DevTools connection is a private pipe too, not a port that other
  programs could connect to.
- The login profile in `~/.local/share/um-vpn/chrome` holds the ADFS and Duo
  sign-in that make logins silent. `um-vpn forget` deletes it.
- Disconnecting logs the session out on the gateway, so a copied cookie stops
  working then.

## Uninstall

```bash
um-vpn forget
rm ~/.local/bin/um-vpn      # or: uv tool uninstall um-vpn
```

## Development

```bash
uv run pytest               # headless Chrome and a fake nmcli: no Duo, no network
pre-commit install          # lint and format on every commit
```

The browser tests skip themselves when no Chrome or Chromium is installed.
[docs/decisions.md](docs/decisions.md) records why um-vpn is built this way, and
what was tried instead.

## License

MIT, see [LICENSE](LICENSE).
