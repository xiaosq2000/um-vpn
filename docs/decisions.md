# Design decisions

Why um-vpn is built the way it is, and what was tried instead. Newest first.

## 2026-09: the UMPASS sign-in from the keyring

`um-vpn remember` stores the UMPASS ID and password in the login keyring, and
`um-vpn on` types them into UM's ADFS form and submits it.

- UM's ADFS page hides **Keep me signed in**
  (`<div id="kmsiArea" style="display:none">`, checked on 2026-09-26), so the
  ADFS sign-in lasts only as long as the browser runs. um-vpn starts a new
  Chrome for every connect, so without a stored sign-in, every connect asks for
  the ID and password.
- UMPASS locks the account, which also holds email, after a few wrong passwords.
  So the password goes to the page in one DevTools call per connect, whatever
  that call returns. A form that already shows an error is never filled, and a
  password that ADFS refuses is deleted from the keyring, so that no later
  connect tries it again.
- The first new page after the submit decides. If it is the sign-in form again,
  ADFS refused the password. A later return to the form, for example after a
  failed Duo prompt, is not blamed on the stored password.
- The password goes only into ADFS's own form (`#loginForm`, `#userNameInput`,
  `#passwordInput`), on a page with the portal's scheme and a host under the
  portal's host minus its first label (`um.edu.mo`). A portal at an IP address,
  or one that leaves a single label, gets no autofill.
- The script runs in an isolated world, whose built-ins the page cannot replace.
  In the same call as the fill, it compares `location.protocol` and
  `location.hostname` with the exact host um-vpn expects, using only `===`. The
  check holds even if a reused context id sends the call to the page's own
  world.
- The script clicks the **Sign in** button instead of submitting the form, so
  UM's own handler runs. That handler adds `@um.edu.mo` to a bare ID.
- The ID and password travel as arguments of a DevTools call over the private
  pipe, never in argv, the environment or the script's source.
- `secret-tool`, from `libsecret-tools`, stores and reads the items. Ubuntu does
  not install it by default. `gdbus` would need the password on its command line
  to store it, and a D-Bus client written with the standard library would take
  several hundred lines.
- The items (`service=um-vpn`, `field=username` or `field=password`) are the
  ones the bash version used, so items it stored still work.
- Chrome's password manager fills the form but does not submit it. A `gpg` file
  or `pass` only trades one unlock prompt for another.

## 2026-09: ESP off, and a keepalive

The tunnel used to stop working about five minutes after every connect, then
recover by itself about sixteen minutes later. um-vpn now turns ESP off and
keeps a small process running that sends one DNS query through the tunnel every
minute. Measured on 2026-09-26 with openconnect 9.12 and
network-manager-openconnect 1.2.10:

- While ESP carries the traffic, the TLS connection to the gateway carries
  nothing. About 300 seconds after the connect, the gateway stopped answering
  ESP, and by then the TLS connection was dead too. Data sent on it was never
  acknowledged, and no FIN or RST ever arrived. Of the sessions logged since the
  rewrite that lasted more than six minutes, seven lost ESP between 5:00 and
  5:27 after the connect, and four lost it sooner.
- openconnect sends no keepalive on the TLS connection of the `nc` protocol.
  That code sits behind `#if 0 /* Not understood for Juniper yet */`, still so
  after 9.21, and openconnect never turns on TCP keepalive. So it moved the
  traffic onto the dead connection, and nothing noticed until the kernel stopped
  retransmitting (`tcp_retries2`), about 1000 seconds later. openconnect then
  reconnected with the same cookie. The gateway's session had not expired, and
  the gateway never sent the idle timeout message that openconnect reports.
- With ESP off and one query a minute, the tunnel stayed up through a 20-minute
  test. For the first ten minutes it carried nothing but the queries, and the
  TLS connection never retransmitted. It then carried a download of about 840 MB
  at about 28 Mbit/s.
- Either change alone is not enough. With ESP on, the queries travel over ESP
  and the TLS connection still sits idle. With ESP off and no queries, the
  tunnel dies after any five minutes without traffic.
- `um-vpn on` starts the process as `um-vpn keepalive`, detached from the
  terminal. It asks the first DNS server that the gateway pushed for the
  portal's own address, and exits once NetworkManager no longer lists the
  tunnel. A lock file keeps it to one process.
- Everything now crosses one TCP connection, which handles packet loss worse
  than ESP does.
- Lowering `tcp_retries2` would shorten the outage, but it needs root and
  changes every TCP connection on the machine.
- Where the five-minute limit sits, in the home network or in front of the
  gateway, is unknown. All of the measured sessions ran on the same home Wi-Fi.

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
  never work again. Every connect signs in afresh, and the cookie is never
  written to disk.
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
