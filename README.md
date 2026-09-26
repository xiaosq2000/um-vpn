# um-vpn

Connect to the University of Macau SSL VPN from a Linux desktop:

```bash
um-vpn remember # once: store your UMPASS ID and password
um-vpn on       # sign in through Chrome and connect
um-vpn off      # disconnect
```

The UM portal signs you in through ADFS and Duo, which no VPN client on Linux
can do by itself. um-vpn opens the portal in Chrome, fills in your UMPASS ID and
password from the login keyring, and waits while you approve Duo. It then takes
the session cookie from the browser and hands it to NetworkManager, which runs
the tunnel with OpenConnect. No sudo.

> **Unofficial.** ICTO neither provides nor supports this. It relies on the
> gateway's legacy Network Connect protocol, which a firmware upgrade could
> switch off.

## Install

um-vpn runs on Ubuntu, or any Linux desktop that uses NetworkManager. It needs
Google Chrome (the `.deb` from google.com) or a Chromium that is not a snap.

Copy the one file onto your `PATH`, and take the NetworkManager plugin and
`secret-tool` from apt:

```bash
sudo apt install network-manager-openconnect libsecret-tools
curl -fLo ~/.local/bin/um-vpn https://raw.githubusercontent.com/xiaosq2000/um-vpn/main/um_vpn.py
chmod +x ~/.local/bin/um-vpn
```

It runs on the system's Python 3.10 or later, which Ubuntu already has, and
`secret-tool` keeps your UMPASS sign-in in the login keyring.

Or install it with [pixi](https://pixi.sh), which brings its own Python and
`secret-tool`, so that only the NetworkManager plugin comes from apt:

```bash
sudo apt install network-manager-openconnect
pixi global install --git https://github.com/xiaosq2000/um-vpn
```

`pixi global update um-vpn` updates it.

## Use

| Command           | Effect                                                                         |
| ----------------- | ------------------------------------------------------------------------------ |
| `um-vpn`          | Show help                                                                      |
| `um-vpn on`       | Sign in through Chrome and connect                                             |
| `um-vpn off`      | Disconnect, which also ends your portal session                                |
| `um-vpn status`   | Show whether the tunnel is up, and its address                                 |
| `um-vpn remember` | Store your UMPASS ID and password in the login keyring                         |
| `um-vpn forget`   | Delete the NetworkManager connection, the login profile and the stored sign-in |

GNOME's quick settings show the tunnel as **University of Macau** under VPN, and
can disconnect it.

While the tunnel is up, `um-vpn on` leaves a small `um-vpn keepalive` process
running. It sends one DNS query through the tunnel each minute, because the
tunnel stops working after about five minutes without traffic, and it exits by
itself once the tunnel is down.

Run `um-vpn remember` once. It asks for your UMPASS ID and password in the
terminal and stores them in the login keyring, which your desktop session
unlocks. From then on, `um-vpn on` opens a Chrome window on the portal, fills in
the UMPASS sign-in and submits it. You approve Duo when it asks, and the window
closes by itself once you are in. If Duo offers to remember the device, accept,
and later connects skip Duo until that expires.

Chrome offers to save the password after um-vpn fills it in. um-vpn does not
need Chrome's copy, so choose **Never**, and Chrome stops asking.

Without a stored sign-in, you type your UMPASS ID and password into the window
on every connect, because UM's sign-in page does not offer "Keep me signed in".

After you change your UMPASS password, run `um-vpn remember` again. If UMPASS
refuses the stored password, `um-vpn on` deletes it, so that it is never tried
twice, and leaves the window for you to finish by hand.

## Configuration

Nothing needs setting for UM. Two environment variables change the defaults:

| Variable            | Default                          | Purpose                          |
| ------------------- | -------------------------------- | -------------------------------- |
| `UM_VPN_PORTAL_URL` | `https://sslvpn.um.edu.mo`       | Use another Pulse/Ivanti portal  |
| `UM_VPN_BROWSER`    | the first Chrome found on `PATH` | Sign in with a different browser |

## Troubleshooting

- **"Chrome is already running on um-vpn's profile"**: a login window is still
  open. Close it and run `um-vpn on` again.
- **"UMPASS did not accept the stored ID and password"**: your UMPASS password
  has changed, or `um-vpn remember` stored a typo. um-vpn has deleted the stored
  pair. Finish signing in by hand in the window, then run `um-vpn remember`.
- **"The sign-in page already shows an error"**: ADFS showed an error, such as a
  locked account, before um-vpn typed anything. um-vpn leaves the form to you
  and keeps the stored password.
- **"secret-tool not found"**: run `sudo apt install libsecret-tools`, or
  install um-vpn with pixi, which brings its own.
- **To stop the autofill** but keep the login profile, delete only the stored
  sign-in with `secret-tool clear service um-vpn`.
- **"NetworkManager could not connect"**: um-vpn prints OpenConnect's own
  explanation below the error. "Cookie was rejected by server" right after a
  login usually means the session was logged out in another window; run
  `um-vpn on` again.
- **Connected, but nothing gets through**: `pgrep -af 'um.vpn.* keepalive'`
  should show one process. If it shows none, run `um-vpn off` and `um-vpn on`.
  [docs/debugging.md](docs/debugging.md) shows how to dig further.
- **A "VPN authentication" dialog from GNOME**: that is NetworkManager's own
  login, which cannot sign in to UM. Cancel it and use `um-vpn on`.
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
- NetworkManager disconnects VPNs on suspend. Run `um-vpn on` again after
  resume.
- The autofill finds UM's ADFS form by its element IDs. If UM changes the page,
  um-vpn stops filling it in, and you type the sign-in yourself.

## Security

- The `DSID` cookie is your live portal session. It goes from Chrome to
  NetworkManager through pipes; it is never written to disk or put on a command
  line, where `ps` would show it to every user on the machine.
- Chrome's DevTools connection is a private pipe too, not a port that other
  programs could connect to.
- Your UMPASS ID and password live in the login keyring. um-vpn reads them with
  `secret-tool` and sends them to Chrome over the DevTools pipe, never on a
  command line or in the environment.
- um-vpn types them only into UM's ADFS sign-in form, on an `https://` page
  under the portal's parent domain, `um.edu.mo`. The page is checked from inside
  a separate script context that the page's own scripts cannot change.
- UMPASS locks your account, email included, after a few wrong passwords. So
  um-vpn submits the form at most once per connect, never fills a form that
  already shows an error, and deletes a stored password that UMPASS refuses.
- The login profile in `~/.local/share/um-vpn/chrome` holds Duo's remembered
  device. `um-vpn forget` deletes it, along with the stored sign-in.
- Disconnecting logs the session out on the gateway, so a copied cookie stops
  working then.

## Uninstall

```bash
um-vpn forget
rm ~/.local/bin/um-vpn      # or: pixi global uninstall um-vpn
```

## Development

```bash
pixi run test               # headless Chrome, fake nmcli and keyring: no Duo, no network
pixi run lint               # every pre-commit hook, on every file
pixi run pre-commit install # lint and format on every commit
```

The browser tests skip themselves when no Chrome or Chromium is installed.
[docs/decisions.md](docs/decisions.md) records why um-vpn is built this way, and
what was tried instead.

## License

MIT, see [LICENSE](LICENSE).
