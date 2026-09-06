"""What a signature on a board post covers, and how it is computed.

The bytes that get signed have to be derived identically on both sides -- by
`capctl` inside a sandbox and by the kernel outside it -- from a value that has
been through JSON and back. So there is exactly one function that produces them,
and both sides call it. Duplicating the rule in the guest tool is how a
signature scheme quietly stops verifying six months later.

**What is covered:** the board, the author, and the payload. Not the post id or
the timestamp, because the kernel assigns those after the signature is made, and
a scheme where the signer cannot reproduce what it signed is not a scheme.

**What that means.** The signature says "this container wrote this text, for
this board". It does not say when, and it does not bind the post to its position
in the conversation: the same signed post replayed onto the same board by
someone holding the same capability would verify again. Within capwrap that is
not reachable -- the kernel takes the author from the socket, so nobody can post
as someone else in the first place -- and outside it, the property that is
wanted is "this agent really said this", which is what this gives.
"""

from __future__ import annotations

import binascii
import hashlib
import json
from typing import Any

from ..guest import ed25519


def _canonical(tag: bytes, fields: dict) -> bytes:
    """Canonical JSON under a domain tag.

    Sorted keys, no incidental whitespace, and `ensure_ascii`, so the encoding
    cannot differ between two Pythons with different defaults. Values go in as
    JSON rather than as text, so a string payload and a number that prints the
    same are different messages.

    The tag is domain separation, and it is not decoration: without it a board
    post's signature could be presented as a signature on a direct message with
    the same text, and both would verify. Each kind of thing that gets signed
    gets its own tag.
    """
    document = json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return tag + b"\n" + document.encode()


def signing_bytes(board: str, author: str, payload: Any) -> bytes:
    """The exact bytes a board post's signature covers."""
    return _canonical(
        b"capwrap-board-v1", {"board": board, "from": author, "payload": payload}
    )


def message_bytes(author: str, payload: Any) -> bytes:
    """The exact bytes a message's signature covers.

    The recipient is deliberately *not* included. A signature that named its
    recipient would need one signature per recipient of a broadcast, and could
    not survive being forwarded -- and forwarding is precisely where the
    signature earns its place: an orchestrator relaying a worker's report should
    not be able to alter it on the way past, and the agent receiving it should be
    able to tell.

    The cost is that a holder of a capability on you could replay a message you
    sent them to someone else, still signed. What the signature claims is
    therefore "this agent wrote this", not "this agent sent this to you".
    """
    return _canonical(b"capwrap-message-v1", {"from": author, "payload": payload})


def sign_post(seed: bytes, board: str, author: str, payload: Any) -> str:
    """Sign a post, returning a hex signature."""
    return ed25519.sign(seed, signing_bytes(board, author, payload)).hex()


def verify_post(
    public_key: str, board: str, author: str, payload: Any, signature: str
) -> bool:
    """Whether a hex signature really is this author's, over this post."""
    return _verify(public_key, signing_bytes(board, author, payload), signature)


def _verify(public_key: str, message: bytes, signature: str) -> bool:
    try:
        key = binascii.unhexlify(public_key)
        raw = binascii.unhexlify(signature)
    except (binascii.Error, ValueError, TypeError):
        return False
    return ed25519.verify(key, message, raw)


def sign_message(seed: bytes, author: str, payload: Any) -> str:
    """Sign a direct message, returning a hex signature."""
    return ed25519.sign(seed, message_bytes(author, payload)).hex()


def verify_message(public_key: str, author: str, payload: Any, signature: str) -> bool:
    """Whether a hex signature really is this author's, over this message."""
    return _verify(public_key, message_bytes(author, payload), signature)


def fingerprint(public_key: str) -> str:
    """A short readable name for a key, for the console and `capctl whoami`."""
    try:
        return hashlib.sha256(binascii.unhexlify(public_key)).hexdigest()[:16]
    except (binascii.Error, ValueError, TypeError):
        return ""
