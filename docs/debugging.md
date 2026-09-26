# Debugging

How to find out why the sign-in was not filled in, or why the tunnel failed to
connect, dropped, or stopped passing traffic. Every command here only reads
state, so it costs no Duo push and leaves the network alone.

## Did um-vpn fill in the sign-in?

`um-vpn on` reports what it did with the stored sign-in:

| Note                                                 | Meaning                                                                        |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| `Filled in the stored UMPASS sign-in.`               | um-vpn submitted the ADFS form.                                                |
| `UMPASS did not accept the stored ID and password …` | ADFS showed the sign-in form again after the submit. um-vpn deleted the pair.  |
| `The sign-in page already shows an error …`          | ADFS showed an error before um-vpn typed anything, so it typed nothing.        |
| `Could not fill in the sign-in …`                    | The page changed between the check and the fill.                               |
| None of these                                        | No sign-in is stored, or no page matched ADFS's form on `https://*.um.edu.mo`. |

To see whether a sign-in is stored, print the stored ID alone. No output means
nothing is stored. Don't use `secret-tool search`, which prints the password as
well.

```bash
secret-tool lookup service um-vpn field username
```

um-vpn finds the form by its element IDs. This shows whether UM's page still has
them, and whether it still hides "Keep me signed in" (`kmsiArea`):

```bash
curl -sL https://sslvpn.um.edu.mo/ | grep -oE 'id="(loginForm|userNameInput|passwordInput|submitButton|errorText|kmsiArea)"( style="[^"]*")?'
```

## Did it connect?

`um-vpn status` shows whether NetworkManager has the tunnel up. When `um-vpn on`
fails, it quotes openconnect's last few lines. For the whole account, read
openconnect's log:

```bash
journalctl -b --no-pager _COMM=openconnect
```

This catches both the lines openconnect prints before the tunnel is up, which
the journal files under NetworkManager, and the ones it sends to syslog
afterwards.

## How did a session end?

openconnect logs one of these as it exits:

| Line                                                | Meaning                                                                      |
| --------------------------------------------------- | ---------------------------------------------------------------------------- |
| `User cancelled (SIGINT/SIGTERM); exiting.`         | Something on this machine disconnected: `um-vpn off`, GNOME, or suspend.     |
| `Server terminated connection (idle timeout)`       | The gateway ended the session. `(session expired)` is the other variant.     |
| `Cookie was rejected by server; exiting.`           | The session was no longer valid, usually because it was logged out.          |
| `Read error on SSL session`, then `SSL negotiation` | The TLS connection died, openconnect reconnected, and the session continues. |

When the tunnel went down locally, NetworkManager records which process asked
for it, by `pid` and `uid`:

```bash
journalctl -b --no-pager -u NetworkManager | grep connection-deactivate
```

## Is traffic crossing the tunnel?

A tunnel can be up in NetworkManager and still pass nothing. Before um-vpn
turned ESP off, this happened about five minutes into every session, and
[decisions.md](decisions.md) explains why. Signs of a dead tunnel:

- openconnect's TLS socket is retransmitting. Take the gateway's address from
  the journal line `Connected to <address>:443`, then run
  `ss -tni dst <address>`. On a dead connection, `backoff:` keeps growing,
  `unacked:` stays above zero, and `bytes_received` stops changing.
- The tunnel's receive counter, `/sys/class/net/vpn0/statistics/rx_packets`,
  stops rising.
- Some minutes later, the journal shows `Read error on SSL session` as
  openconnect gives up on the dead connection.

While the tunnel is up, exactly one keepalive should be running:

```bash
pgrep -af 'um.vpn.* keepalive'
```

If none is, disconnecting and connecting again starts one.

## Lines that look wrong but are not

- `Failed to open /dev/vhost-net: Permission denied` appears at every connect.
  openconnect then uses the tunnel device directly.
- After a disconnect, NetworkManager keeps an idle `vpn0` device for the next
  connect.
