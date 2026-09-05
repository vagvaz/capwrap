"""The terminal console.

Most of it is drawing, which is checked by running it. What is worth unit
testing is the frame parser: it is the one piece where being subtly wrong
corrupts an agent's screen rather than failing loudly.
"""

from __future__ import annotations

import json
import struct

import pytest

from capwrap.tui.app import VIEWS, Console
from capwrap.tui.client import Client, ConsoleError
from capwrap.tui.ws import WebSocket


class FakeSocket:
    """A socket that hands back a scripted byte stream."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)
        self.sent = bytearray()

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        pass


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """A frame as the daemon would send it: unmasked, since servers do not mask."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < (1 << 16):
        header.append(126)
        header += struct.pack("!H", len(payload))
    else:
        header.append(127)
        header += struct.pack("!Q", len(payload))
    return bytes(header) + payload


# ==========================================================================
# framing
# ==========================================================================


def test_a_binary_frame_comes_back_whole():
    ws = WebSocket(FakeSocket(server_frame(0x2, b"terminal output")))
    assert ws.receive() == [(0x2, b"terminal output")]


def test_several_frames_in_one_read_all_come_back():
    """A PTY burst arrives as one read holding several frames."""
    ws = WebSocket(FakeSocket(
        server_frame(0x2, b"one") + server_frame(0x2, b"two")
        + server_frame(0x2, b"three")
    ))
    assert ws.receive() == [(0x2, b"one"), (0x2, b"two"), (0x2, b"three")]


def test_a_frame_split_across_reads_waits_for_its_tail():
    """TCP does not respect frame boundaries, so neither can the parser."""
    whole = server_frame(0x2, b"a longer piece of terminal output")
    sock = FakeSocket(whole[:6], whole[6:])
    ws = WebSocket(sock)
    assert ws.receive() == []                    # not yet a whole frame
    assert ws.receive() == [(0x2, b"a longer piece of terminal output")]


def test_a_fragmented_message_is_reassembled():
    """Dropping the tail would corrupt the screen rather than fail loudly."""
    ws = WebSocket(FakeSocket(
        server_frame(0x2, b"first half ", fin=False)
        + server_frame(0x0, b"second half", fin=True)
    ))
    assert ws.receive() == [(0x2, b"first half second half")]


def test_a_long_payload_uses_the_extended_length_and_survives():
    payload = b"x" * 70000
    ws = WebSocket(FakeSocket(server_frame(0x2, payload)))
    assert ws.receive() == [(0x2, payload)]


def test_a_ping_is_answered_and_not_handed_to_the_caller():
    sock = FakeSocket(server_frame(0x9, b"are you there"))
    ws = WebSocket(sock)
    assert ws.receive() == []
    # A pong, masked as a client must.
    assert sock.sent[0] & 0x0F == 0xA
    assert sock.sent[1] & 0x80, "a client must mask every frame it sends"


def test_the_peer_going_away_reads_as_a_close():
    """One thing for the caller to check rather than two."""
    ws = WebSocket(FakeSocket())
    assert ws.receive() == [(0x8, b"")]


def test_what_the_client_sends_is_masked_and_round_trips():
    sock = FakeSocket()
    ws = WebSocket(sock)
    ws.send_text(json.dumps({"type": "input", "data": "ls\r"}))

    sent = bytes(sock.sent)
    assert sent[0] == 0x81                       # fin + text
    assert sent[1] & 0x80                        # masked
    length = sent[1] & 0x7F
    mask, body = sent[2:6], sent[6 : 6 + length]
    decoded = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
    assert json.loads(decoded) == {"type": "input", "data": "ls\r"}


# ==========================================================================
# the console's own behaviour
# ==========================================================================


class StubClient(Client):
    def __init__(self, **data):
        super().__init__()
        self.data = data
        self.answered: list[tuple] = []

    def overview(self):
        return self.data.get("overview", {"containers": [], "approvals": []})

    def screens(self, rows=40):
        return self.data.get("screens", [])

    def boards(self):
        return self.data.get("boards", [])

    def audit(self, limit=100, denied=False):
        return self.data.get("audit", [])

    def answer(self, approval_id, decision, reason="", rights=None):
        self.answered.append((approval_id, decision, rights))


def approval(id: int, container: str, kind: str | None = None, **request):
    context = {"kind": kind} if kind else {}
    if request:
        context["request"] = request
    return {"id": id, "container": container, "question": "?", "context": context}


def test_tab_cycles_the_views():
    console = Console(StubClient())
    for expected in VIEWS[1:] + VIEWS[:1]:
        console.handle(ord("\t"))
        assert console.view == expected


def test_answering_takes_the_approval_off_the_queue():
    client = StubClient(overview={
        "containers": [], "approvals": [approval(7, "alpha")],
    })
    console = Console(client)
    console.refresh(rows=10)
    console.view = "approvals"

    console.handle(ord("y"))
    assert client.answered == [(7, "allow", None)]
    assert console.approvals == []


def test_allowing_a_capability_request_grants_what_was_asked_for():
    """The rights picker is a browser affordance; here it is as asked, or deny.

    Granting a *narrower* set than requested is a judgement that wants the
    checkbox list. Silently granting something other than what the card shows
    would be worse than not offering the choice at all.
    """
    client = StubClient(overview={"containers": [], "approvals": [
        approval(3, "beta", kind="capability_request",
                 kind_="container", rights=["send", "inspect"]),
    ]})
    console = Console(client)
    console.refresh(rows=10)
    console.view = "approvals"
    console.handle(ord("y"))

    assert client.answered == [(3, "allow", ["send", "inspect"])]


def test_denying_never_carries_rights():
    client = StubClient(overview={"containers": [], "approvals": [
        approval(3, "beta", kind="capability_request", rights=["send"]),
    ]})
    console = Console(client)
    console.refresh(rows=10)
    console.view = "approvals"
    console.handle(ord("n"))
    assert client.answered == [(3, "deny", None)]


def test_attaching_to_a_stopped_container_says_so_rather_than_hanging():
    client = StubClient(overview={
        "containers": [{"name": "alpha", "running": False, "state": "exited"}],
        "approvals": [],
    })
    console = Console(client)
    console.refresh(rows=10)
    console.handle(ord("\n"))

    assert console.attach_request is None
    assert "not running" in console.status


def test_attaching_asks_the_caller_to_tear_curses_down_first():
    """The agent's own full-screen program needs the terminal to itself."""
    client = StubClient(overview={
        "containers": [{"name": "alpha", "running": True}], "approvals": [],
    })
    console = Console(client)
    console.refresh(rows=10)
    console.handle(ord("\n"))
    assert console.attach_request == "alpha"


def test_a_daemon_that_is_not_there_is_reported_not_raised():
    console = Console(Client(port=1))
    console.refresh(rows=10)
    assert "no capwrap answering" in console.error
    assert console.running is True


def test_selection_survives_a_container_disappearing():
    client = StubClient(overview={
        "containers": [{"name": n, "running": True} for n in ("a", "b", "c")],
        "approvals": [],
    })
    console = Console(client)
    console.refresh(rows=10)
    console.selected = 2

    client.data["overview"] = {
        "containers": [{"name": "a", "running": True}], "approvals": [],
    }
    console.refresh(rows=10)
    assert console.current["name"] == "a"
