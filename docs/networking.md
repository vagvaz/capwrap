# Networking

Network access is a capability like any other. A container reaches exactly the
destinations it holds a rule for, and nothing else.

## The three modes

| config | namespace | what the agent can reach |
|---|---|---|
| nothing (default) | `--unshare-net` | nothing at all — loopback, no route |
| `[[caps.network]]` rules | `--unshare-net` | only what its rules match, through the proxy |
| `sandbox.network = true` | *(shared)* | **the host's whole network stack** |

The third is the escape hatch and is worth being uneasy about: it is not
"network access", it is the host's network stack, including the LAN, any VPN the
host is on, and every service on the host's loopback — the capwrap console
among them. Writing both `network = true` and a rule list is refused rather than
resolved, because no proxy can constrain a container that has its own route out,
and a config that read as restricted while not being restricted is the worst of
the available outcomes.

## Writing rules

```toml
name = "builder"

[[caps.network]]
name    = "pypi"
pattern = '(pypi\.org|files\.pythonhosted\.org):443'

[[caps.network]]
name    = "anthropic"
pattern = 'api\.anthropic\.com:443'
```

A pattern is a regex over `host:port`, **anchored at both ends** before it is
used. That anchoring is not a detail: unanchored, `pypi\.org:443` would also
accept `pypi.org:443.attacker.example`, which is the opposite of what the person
writing the rule believed it said.

Ports are part of the destination. `example.com:443` does not grant
`example.com:22`, because HTTPS to a host and SSH to the same host are not the
same authority.

Each rule is a separate kernel object and the container holds a separate
capability on each. That is what makes narrowing work — see below.

## How it works

A proxied container still runs under `--unshare-net`: loopback and no route
anywhere. It cannot reach the host, so it cannot reach the proxy either — over
the network. What it does have is an `AF_UNIX` socket bind-mounted into its
filesystem, because unix sockets are filesystem objects and cross a boundary
that nothing else does.

Tooling does not speak proxy-over-unix-socket, though: `HTTPS_PROXY` wants a host
and a port. So the container's entry point is a small relay
(`capwrap/guest/netrelay.py`) that listens on `127.0.0.1:8118` *inside* the
sandbox and forwards to the socket, then execs the agent. The result is an
ordinary HTTP proxy that curl, pip, git and node all understand, backed by a
channel the container could not have opened for itself.

```
  agent → 127.0.0.1:8118 → netrelay → /run/capwrap-proxy.sock
                                            │  (the sandbox boundary)
                                            ▼
                              NetProxy → kernel.net_allows() → the internet
```

**Identity comes from the socket, again.** The daemon binds one proxy socket per
container and the handler closes over the container name, exactly as the control
socket works. Nothing in an HTTP request establishes who is asking, so there is
nothing for an agent to forge — and one container's proxy socket is never
mounted into another's sandbox.

The relay runs as the entry point rather than as a second supervised process, so
it cannot outlive the agent it exists for. It ignores `SIGINT`, because Ctrl-C at
the terminal is delivered to the whole foreground process group and tearing the
proxy down underneath the agent is not what the operator meant by it.

## What a rule can honestly say

Only `host:port`. The proxy does not terminate TLS: for HTTPS it sees a `CONNECT`
line and then ciphertext, and it neither mints certificates nor reads bodies.
That bounds what a rule can express — there is no way to say "this path but not
that one" over HTTPS — and it is a deliberate trade. The agent's traffic stays
encrypted end-to-end to the site it is talking to, and capwrap cannot read it
even though it is carrying it.

Plain HTTP arrives as an absolute-URI request and does carry a path, but rules
are still matched on `host:port` alone. A rule that meant one thing over HTTP and
another over HTTPS would be a trap.

## Narrowing, and why rules are separate objects

Delegation may only ever shrink authority. For rules that means handing on a
*subset of the rules you hold*, never a narrowed pattern:

```bash
capctl caps                      # which slot is which
capctl grant 4 6 --rights connect   # give slot-4's holder my "pypi" rule
```

The obvious alternative — one network capability carrying a list of patterns,
narrowed on delegation — would require deciding whether one regex is contained
in another. That is undecidable in general, and a security model should not rest
on a question nobody can answer. One rule, one object, one capability makes
narrowing an ordinary delegation, checked by the same mapping database as
everything else, and revocation recursive in the same way.

## Asking for more

A denial is an HTTP 403 with the reason in the body, not a dropped connection:

```
netty holds no network capability for pypi.org:80

Rules held: example, anthropic-docs
Ask the operator for one with:
  capctl request net_rule '<name>=<host:port regex>' --reason '...'
```

Which the agent can then do, and the operator answers in the console:

```bash
capctl net          # what may I reach, and through which rule?
capctl request net_rule 'pypi=(pypi\.org|files\.pythonhosted\.org):443' \
  --reason 'pip install needs the package index'
```

Approving it performs the delegation, and the rule is live for the next
connection — no restart. Revoking it in the console closes the hole just as
immediately, and recursively: anything the holder passed on dies with it.

Every decision is audited either way. The denials are the interesting half, since
they are how you find out an agent has been trying to reach somewhere it should
not:

```
DENY  netty  pypi.org:443     {"held_rules": ["example", "anthropic-docs"]}
DENY  netty  example.com:8443 {"held_rules": ["example", "anthropic-docs"]}
```

## What was considered instead

- **User-mode networking (`slirp4netns`, `pasta`).** A real TCP/IP stack per
  container, as rootless Podman does. Filtering is coarser than a proxy's and
  cannot see names at all, and it costs a process and a dependency per container.
- **veth pair + nftables.** The most conventional filtering, and the strongest by
  address. Both creating the interface and writing host firewall rules need
  `CAP_NET_ADMIN`, so the daemon would have to be privileged.
- **DNS-only filtering.** Cheap, and worth almost nothing alone: it stops name
  resolution, not connections, so anything with a literal IP walks past it.

Filtering *by name* is inherently approximate — names resolve to addresses that
change, several names share an address, and SNI is an in-band hint the agent
controls. A proxy that terminates the connection is the only one of these that
can honestly say "this agent may reach api.anthropic.com and nothing else",
which is why it is the one that got built.

## Still open

`sandbox.network = true` shares the host's netns, and the console is on a TCP
port in it with no authentication. A container in that mode can grant itself
capabilities through the web API. Rule-based access does not have this problem —
there is no route to the host at all — but the escape hatch still does.
Authenticating the console, or moving it onto a unix socket, closes it.
