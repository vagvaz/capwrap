"""The network capability proxy.

A container with network rules gets no network namespace of its own to speak of
-- ``--unshare-net`` leaves it with loopback and nothing else -- and reaches the
outside world only through this proxy.  Which means the question "may this agent
talk to pypi?" is answered in the same place as every other question about
authority: the capability kernel.

**Identity comes from the socket, again.**  The proxy binds one ``AF_UNIX``
socket per container, exactly as the control socket does, and the handler closes
over the container name.  Nothing in an HTTP request establishes who is asking,
so there is nothing for an agent to forge, and an agent cannot reach another
container's proxy socket because it is never mounted into its sandbox.

**Only ``host:port`` is ever inspected.**  For HTTPS the proxy sees a CONNECT
line and then ciphertext; it does not terminate TLS, mint certificates, or read
request bodies.  That bounds what a rule can honestly say -- there is no way to
express "this path but not that one" over HTTPS -- and it keeps the agent's
traffic end-to-end encrypted to the site it is talking to, with capwrap unable
to read it even though it is carrying it.

Plain HTTP arrives as an absolute-URI request line, which does carry a path, but
rules are still matched on ``host:port`` alone.  A rule that meant one thing over
HTTP and another over HTTPS would be a trap.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Callable, Protocol

#: Refuse a request line longer than this rather than buffering it. A proxy
#: client that never sends a newline must not be able to grow the daemon.
MAX_HEADER_BYTES = 64 * 1024

#: How long to wait for the upstream TCP connection before giving up.
CONNECT_TIMEOUT = 30.0

#: Copied between sockets in chunks this size once a tunnel is established.
CHUNK = 64 * 1024


class Decider(Protocol):
    """What the proxy needs from the kernel: a yes or no, already audited."""

    def __call__(self, container: str, host: str, port: int) -> dict: ...


class ProxyError(Exception):
    """A malformed or unsupported proxy request."""


def parse_authority(authority: str, default_port: int) -> tuple[str, int]:
    """Split ``host:port``, including the bracketed IPv6 form.

    A missing port is filled in from the scheme, because a rule is written
    against a port and a request without one still has a real destination.
    """
    authority = authority.strip()
    if not authority:
        raise ProxyError("no destination in the request")

    if authority.startswith("["):                       # [::1]:443
        close = authority.find("]")
        if close < 0:
            raise ProxyError("unterminated IPv6 literal")
        host = authority[1:close]
        rest = authority[close + 1 :]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port

    host, sep, tail = authority.rpartition(":")
    if sep and tail.isdigit():
        return host, int(tail)
    return authority, default_port


def target_of(method: str, target: str) -> tuple[str, int]:
    """The ``host, port`` a proxy request is asking to reach."""
    if method.upper() == "CONNECT":
        return parse_authority(target, 443)

    lowered = target.lower()
    for scheme, default in (("http://", 80), ("https://", 443)):
        if lowered.startswith(scheme):
            rest = target[len(scheme) :]
            authority = rest.split("/", 1)[0].split("?", 1)[0]
            # Strip any userinfo: it is not part of the destination, and letting
            # it through would let `pypi.org@evil.example` read as pypi.
            if "@" in authority:
                authority = authority.rpartition("@")[2]
            return parse_authority(authority, default)

    raise ProxyError(
        "this proxy only accepts CONNECT or an absolute http:// or https:// URL"
    )


class NetProxy:
    """One container's window onto the network."""

    def __init__(
        self,
        container: str,
        decide: Decider,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.container = container
        self.decide = decide
        self.on_event = on_event
        self.server: asyncio.AbstractServer | None = None

    async def start(self, socket_path: Path) -> asyncio.AbstractServer:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self._handle, path=str(socket_path)
        )
        import os

        # Same reasoning as the control socket: the sandbox runs as this uid, so
        # 0600 keeps other local users out without getting in the agent's way.
        os.chmod(socket_path, 0o600)
        return self.server

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.server.wait_closed(), timeout=2.0)

    # ------------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await self._read_head(reader)
            if head is None:
                return
            request_line, headers = head
            method, target, version = _split_request_line(request_line)
            host, port = target_of(method, target)
        except ProxyError as exc:
            await self._refuse(writer, 400, str(exc))
            return
        except (ValueError, asyncio.IncompleteReadError, ConnectionResetError):
            await self._refuse(writer, 400, "malformed proxy request")
            return

        verdict = self.decide(self.container, host, port)
        self._emit(host, port, verdict)
        if not verdict.get("allowed"):
            await self._refuse(
                writer, 403,
                f"{self.container} holds no network capability for {host}:{port}",
                held=verdict.get("held_rules") or [],
            )
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
            await self._refuse(writer, 502, f"cannot reach {host}:{port}: {exc}")
            return

        try:
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(
                    _origin_form(method, target, version, headers)
                )
                await upstream_writer.drain()
            await _splice(reader, writer, upstream_reader, upstream_writer)
        finally:
            for w in (upstream_writer, writer):
                w.close()
                with contextlib.suppress(Exception):
                    await w.wait_closed()

    async def _read_head(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, list[str]] | None:
        """The request line and its headers, as text."""
        blob = b""
        while b"\r\n\r\n" not in blob:
            if len(blob) > MAX_HEADER_BYTES:
                raise ProxyError("request header is too large")
            chunk = await reader.read(4096)
            if not chunk:
                return None if not blob.strip() else _fail_incomplete()
            blob += chunk
        text = blob.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = text.split("\r\n")
        return lines[0], lines[1:]

    async def _refuse(
        self, writer: asyncio.StreamWriter, status: int, message: str,
        held: list[str] | None = None,
    ) -> None:
        """Answer with a real HTTP error, so the agent is told *why*.

        A dropped connection would send the agent hunting for a network fault.
        Naming the rules it does hold turns a denial into something it can act
        on -- ask the operator for the one it needs, or stop trying.
        """
        body = message
        if held is not None:
            body += (
                "\n\nRules held: " + (", ".join(held) or "none")
                + "\nAsk the operator for one with:"
                  "\n  capctl request net_rule '<name>=<host:port regex>' --reason '...'"
            )
        payload = body.encode()
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n".encode() + payload
        )
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    def _emit(self, host: str, port: int, verdict: dict) -> None:
        if self.on_event is None:
            return
        with contextlib.suppress(Exception):
            self.on_event({
                "container": self.container,
                "host": host,
                "port": port,
                "allowed": bool(verdict.get("allowed")),
                "rule": verdict.get("rule"),
            })


def _fail_incomplete() -> None:
    raise ProxyError("the client closed the connection mid-request")


def _split_request_line(line: str) -> tuple[str, str, str]:
    parts = line.split()
    if len(parts) != 3:
        raise ProxyError(f"not a request line: {line[:80]!r}")
    return parts[0], parts[1], parts[2]


#: Headers that belong to one hop and must not be forwarded upstream.
_HOP_BY_HOP = {
    "proxy-connection", "proxy-authorization", "proxy-authenticate",
    "connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade",
}


def _origin_form(method: str, target: str, version: str, headers: list[str]) -> bytes:
    """Rewrite an absolute-URI request into the form an origin server expects.

    ``Connection: close`` is forced in both directions on purpose. Keep-alive
    would let a client send a second absolute-URI request for a *different* host
    down a connection this proxy has already pinned to one upstream, and the
    second destination would never be checked. One request per connection means
    one decision per request.
    """
    path = "/"
    for scheme in ("http://", "https://"):
        if target.lower().startswith(scheme):
            rest = target[len(scheme) :]
            cut = rest.find("/")
            path = rest[cut:] if cut >= 0 else "/"
            break

    kept = [
        h for h in headers
        if h and h.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP
    ]
    kept.append("Connection: close")
    return (
        f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n"
    ).encode("latin-1")


async def _splice(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy bytes both ways until either end goes quiet."""

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.write_eof()

    both = [
        asyncio.ensure_future(pump(client_reader, upstream_writer)),
        asyncio.ensure_future(pump(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait(both, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in both:
            task.cancel()
        for task in both:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
