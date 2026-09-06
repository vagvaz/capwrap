"""Per-container message queues.

A mailbox is where the kernel's `deliver_message` hook actually puts things.
Each container has exactly one, plus the operator, whose mailbox backs the web
UI's inbox.

Messages are also mirrored into the container's `/shared/inbox` as files when
its config asks for it, so an agent that is not polling the socket still notices
that something arrived -- an LLM agent will happily ignore a queue it was never
told to check, but it will read a file it trips over.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: Kept per mailbox for the web UI and for `capctl recv --history`.
HISTORY_LIMIT = 500


@dataclass
class Message:
    id: int
    sender: str
    payload: Any
    kind: str = "message"
    ts: float = field(default_factory=time.time)
    via_slot: int | None = None
    #: Ed25519 signature over (sender, payload), and the key to check it with.
    #: Carried on the message so a recipient can verify without asking anyone,
    #: and so it survives being forwarded.
    signature: str = ""
    public_key: str = ""

    @property
    def signed(self) -> bool:
        return bool(self.signature)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from": self.sender,
            "kind": self.kind,
            "payload": self.payload,
            "ts": self.ts,
            "via_slot": self.via_slot,
            "signature": self.signature,
            "public_key": self.public_key,
            "signed": self.signed,
        }


class Mailbox:
    """An asyncio queue plus a bounded history."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self.history: deque[Message] = deque(maxlen=HISTORY_LIMIT)
        self._next_id = 1
        self._watchers: list[Callable[[Message], None]] = []

    def watch(self, callback: Callable[[Message], None]) -> Callable[[], None]:
        """Subscribe to arrivals; returns an unsubscribe function."""
        self._watchers.append(callback)
        return lambda: self._watchers.remove(callback)

    def post(self, raw: dict) -> Message:
        message = Message(
            id=self._next_id,
            sender=raw.get("from", "?"),
            payload=raw.get("payload"),
            kind=raw.get("kind", "message"),
            via_slot=raw.get("via_slot"),
            signature=raw.get("signature", "") or "",
            public_key=raw.get("public_key", "") or "",
        )
        self._next_id += 1
        self.history.append(message)
        self.queue.put_nowait(message)
        for callback in list(self._watchers):
            try:
                callback(message)
            except Exception:  # noqa: BLE001 - a bad watcher must not break delivery
                pass
        return message

    async def receive(
        self, timeout: float | None = None, limit: int = 1
    ) -> list[Message]:
        """Pop up to `limit` messages, waiting up to `timeout` for the first.

        `timeout=0` polls without blocking; `None` waits indefinitely.
        """
        out: list[Message] = []
        if timeout == 0:
            while len(out) < limit and not self.queue.empty():
                out.append(self.queue.get_nowait())
            return out

        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return out
        out.append(first)
        while len(out) < limit and not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out

    def recent(self, limit: int = 50) -> list[Message]:
        return list(self.history)[-limit:]

    def queued(self) -> list[Message]:
        """The messages still waiting to be received, in arrival order.

        Distinct from `recent`, which is the *history* (delivered messages
        included). The console's mailbox section shows what is still queued, so
        the operator can nudge or discard exactly those.
        """
        out: list[Message] = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        for message in out:
            self.queue.put_nowait(message)
        return out

    def discard(self, message_id: int) -> bool:
        """Remove a queued message without delivering it.

        The operator's console offers this so a message that was posted by
        mistake -- or that the operator has already dealt with out of band --
        can be dropped rather than left to nag the agent. Only messages still
        waiting in the queue are affected; one already received is gone.
        """
        removed = False
        kept: list[Message] = []
        while not self.queue.empty():
            message = self.queue.get_nowait()
            if message.id == message_id:
                removed = True
            else:
                kept.append(message)
        for message in kept:
            self.queue.put_nowait(message)
        return removed

    @property
    def pending(self) -> int:
        return self.queue.qsize()


class MailboxRegistry:
    """All mailboxes, created on demand."""

    def __init__(self) -> None:
        self._boxes: dict[str, Mailbox] = {}

    def get(self, owner: str) -> Mailbox:
        box = self._boxes.get(owner)
        if box is None:
            box = self._boxes[owner] = Mailbox(owner)
        return box

    def drop(self, owner: str) -> None:
        self._boxes.pop(owner, None)

    def names(self) -> Iterable[str]:
        return self._boxes.keys()


def write_inbox_file(shared_dir: Path, message: Message) -> Path:
    """Drop a message into a container's /shared/inbox as a readable file.

    Named with a zero-padded id so a plain `ls` shows them in arrival order.
    """
    inbox = shared_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{message.id:04d}-from-{message.sender}.json"
    path.write_text(json.dumps(message.to_dict(), indent=2) + "\n")
    return path
