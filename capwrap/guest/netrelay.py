#!/usr/bin/env python3
"""Give the sandbox a proxy it can name, without giving it a network.

A container with network rules runs under ``--unshare-net``: it has loopback and
nothing else, so it cannot reach the host at all.  The capability proxy is on the
other side of an ``AF_UNIX`` socket bind-mounted into the sandbox -- unix sockets
are filesystem objects, not network ones, so they cross that boundary when
nothing else does.

What they do not cross is tooling.  ``HTTP_PROXY`` takes a host and a port, and
no HTTP client speaks proxy-over-unix-socket.  So this listens on loopback
*inside* the sandbox and forwards every connection to the socket.  The result is
an ordinary ``http://127.0.0.1:<port>`` proxy that curl, pip, git and node all
understand, backed by a channel the container could not have opened itself.

The alternative would be for the daemon to enter the container's network
namespace and bind a port there, which needs CAP_SYS_ADMIN on the host.  This
needs nothing.

Runs as the container's entry point and execs the real command as a child, so
there is no second process to supervise and the relay cannot outlive the agent:

    netrelay.py --port 8118 --socket /run/capwrap-proxy.sock -- claude

Standard library only, single file: the guest tools directory is mounted
read-only into an otherwise unrelated filesystem and cannot import capwrap.
"""

from __future__ import annotations

import argparse
import signal
import socket
import socketserver
import subprocess
import sys
import threading

#: Copied between sockets in chunks this size.
CHUNK = 64 * 1024


class _Handler(socketserver.BaseRequestHandler):
    """One loopback connection, spliced onto one unix-socket connection."""

    socket_path: str = ""

    def handle(self) -> None:
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(self.socket_path)
        except OSError:
            # The daemon is gone or never bound. Closing is the honest answer;
            # inventing an HTTP error here would be guessing at the protocol.
            return
        try:
            pump = threading.Thread(
                target=_copy, args=(self.request, upstream), daemon=True
            )
            pump.start()
            _copy(upstream, self.request)
            pump.join(timeout=5)
        finally:
            for sock in (upstream, self.request):
                try:
                    sock.close()
                except OSError:
                    pass


def _copy(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(CHUNK)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        # Half-close, so the far end sees EOF rather than waiting on a socket
        # nothing will ever write to again.
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Relay(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int, socket_path: str, host: str = "127.0.0.1") -> _Relay:
    handler = type("Handler", (_Handler,), {"socket_path": socket_path})
    relay = _Relay((host, port), handler)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    return relay


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="netrelay",
        description="Expose a unix-socket proxy on loopback, then run a command.",
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command to run is required, after --")

    try:
        serve(args.port, args.socket, host=args.host)
    except OSError as exc:
        # Better a container with no network than one that silently believes it
        # has none of the restrictions either.
        print(f"capwrap: could not start the network relay: {exc}", file=sys.stderr)

    # The agent owns the terminal. Ctrl-C at the PTY is delivered to the whole
    # foreground process group, so it arrives here too -- ignore it and let the
    # agent decide what an interrupt means, rather than tearing down its proxy
    # underneath it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    child = subprocess.Popen(command)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGHUP, forward)

    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:  # SIG_IGN does not cover every path
            continue


if __name__ == "__main__":
    sys.exit(main())
