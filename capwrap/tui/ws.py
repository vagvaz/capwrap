"""Just enough WebSocket to carry a terminal.

The daemon already speaks WebSocket because the browser needs it, and the
terminal console attaches to the same endpoint rather than growing a second
protocol beside it -- one server path, one set of semantics, one thing to keep
working.

What is here is a client for exactly that: connect, send text frames, read
binary ones, close. No extensions, no compression, no fragmentation on the way
out. It is small because the job is small, and writing it avoids a dependency on
the read side that the async `websockets` package would impose on a synchronous
`select` loop.

Incoming fragmented frames *are* handled: a PTY producing a large burst is a
normal thing for a server to split, and dropping the tail would corrupt the
agent's screen rather than fail loudly.
"""

from __future__ import annotations

import base64
import os
import socket
import struct

#: The fixed GUID from RFC 6455, used in the handshake accept value.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    """A client connection, framed."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buffer = b""
        #: Set while a fragmented message is in flight.
        self._continuation: tuple[int, bytearray] | None = None

    # ------------------------------------------------------------------

    @classmethod
    def connect(
        cls, host: str, port: int, path: str, timeout: float = 10.0
    ) -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("the server closed the connection during the handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise OSError(f"the server refused the upgrade: {status}")

        ws = cls(sock)
        ws._buffer = rest
        sock.settimeout(None)
        return ws

    # ------------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def _send(self, opcode: int, payload: bytes) -> None:
        # A client must mask every frame it sends; the server drops one that is
        # not masked, rather than interpreting it.
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        length = len(payload)

        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        self.sock.sendall(bytes(header) + mask + masked)

    # ------------------------------------------------------------------

    def receive(self) -> list[tuple[int, bytes]]:
        """Whatever has arrived, as (opcode, payload) pairs.

        Non-blocking beyond one read: the caller is in a `select` loop and calls
        this when the socket is readable. A read that yields nothing means the
        peer has gone, which is reported as a close frame so the caller has one
        thing to check rather than two.
        """
        try:
            chunk = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return []
        if not chunk:
            return [(0x8, b"")]
        self._buffer += chunk

        out: list[tuple[int, bytes]] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                return out
            fin, opcode, payload = frame

            if opcode == 0x9:                      # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:                      # pong
                continue

            if opcode == 0x0:                      # continuation
                if self._continuation is None:
                    continue                       # nothing to continue; ignore
                start_opcode, buffered = self._continuation
                buffered += payload
                if fin:
                    self._continuation = None
                    out.append((start_opcode, bytes(buffered)))
                continue

            if not fin:
                self._continuation = (opcode, bytearray(payload))
                continue
            out.append((opcode, payload))

    def _take_frame(self) -> tuple[bool, int, bytes] | None:
        """One whole frame, or None while the buffer is still short of one."""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2

        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = struct.unpack("!H", buffer[offset : offset + 2])[0]
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = struct.unpack("!Q", buffer[offset : offset + 8])[0]
            offset += 8

        mask = b""
        if masked:                                  # servers normally do not
            if len(buffer) < offset + 4:
                return None
            mask = buffer[offset : offset + 4]
            offset += 4

        if len(buffer) < offset + length:
            return None
        payload = buffer[offset : offset + length]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        self._buffer = buffer[offset + length :]
        return fin, opcode, bytes(payload)

    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
