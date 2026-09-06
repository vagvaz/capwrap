"""Signed board posts.

capwrap already knows who wrote a post -- attribution comes from the socket the
request arrived on and cannot be forged from inside a container. A signature is
therefore not how the kernel decides anything. It is what lets a post be checked
*later*: after an export, after a restart, by a reader who was not there, or by
someone who would rather not have to trust the daemon that recorded it.

The Ed25519 implementation itself is checked against the RFC 8032 vectors, since
a signature scheme that is subtly wrong verifies happily until it matters.
"""

from __future__ import annotations

import binascii
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.errors import CapabilityError
from capwrap.guest import ed25519
from capwrap.kernel.kernel import CapKernel
from capwrap.kernel.signing import fingerprint, sign_post, signing_bytes, verify_post


# ==========================================================================
# the primitive
# ==========================================================================

#: RFC 8032 section 7.1. A signature scheme that is subtly wrong still verifies
#: its own output, so the only meaningful check is against somebody else's.
RFC_8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a"
        "33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e"
        "15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d"
        "16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize("seed,public,message,signature", RFC_8032)
def test_ed25519_matches_the_rfc_vectors(seed, public, message, signature):
    seed_bytes = binascii.unhexlify(seed)
    message_bytes = binascii.unhexlify(message)

    assert ed25519.public_key(seed_bytes).hex() == public
    assert ed25519.sign(seed_bytes, message_bytes).hex() == signature
    assert ed25519.verify(
        binascii.unhexlify(public), message_bytes, binascii.unhexlify(signature)
    )


def test_a_changed_message_does_not_verify():
    seed = ed25519.generate_seed()
    key = ed25519.public_key(seed)
    signature = ed25519.sign(seed, b"the build is green")
    assert ed25519.verify(key, b"the build is green", signature)
    assert not ed25519.verify(key, b"the build is red", signature)


def test_another_key_does_not_verify():
    seed = ed25519.generate_seed()
    signature = ed25519.sign(seed, b"mine")
    other = ed25519.public_key(ed25519.generate_seed())
    assert not ed25519.verify(other, b"mine", signature)


@pytest.mark.parametrize(
    "key,signature",
    [
        (b"too short", b"x" * 64),
        (b"x" * 32, b"too short"),
        (b"\xff" * 32, b"\x00" * 64),  # not a point on the curve
    ],
    ids=["short-key", "short-signature", "not-a-point"],
)
def test_malformed_input_is_false_not_an_exception(key, signature):
    """A caller checking a signature wants one answer; corrupt and wrong are
    the same answer to it."""
    assert ed25519.verify(key, b"anything", signature) is False


def test_a_non_canonical_scalar_is_rejected():
    """S must be reduced. Accepting an unreduced one admits malleable variants
    of an otherwise valid signature."""
    seed = ed25519.generate_seed()
    key = ed25519.public_key(seed)
    good = ed25519.sign(seed, b"message")
    mangled = good[:32] + (b"\xff" * 32)
    assert not ed25519.verify(key, b"message", mangled)


# ==========================================================================
# what a post's signature covers
# ==========================================================================


def test_the_signature_covers_the_board_the_author_and_the_payload():
    base = signing_bytes("standup", "alpha", "done")
    assert base != signing_bytes("planning", "alpha", "done")
    assert base != signing_bytes("standup", "beta", "done")
    assert base != signing_bytes("standup", "alpha", "not done")


def test_the_encoding_is_canonical():
    """Two dicts that differ only in key order are the same message."""
    assert signing_bytes("b", "a", {"x": 1, "y": 2}) == signing_bytes(
        "b", "a", {"y": 2, "x": 1}
    )


def test_a_string_payload_and_its_json_are_different_messages():
    """The payload goes in as a JSON value, not as text, so these cannot collide."""
    assert signing_bytes("b", "a", "1") != signing_bytes("b", "a", 1)


def test_sign_and_verify_round_trip():
    seed = ed25519.generate_seed()
    key = ed25519.public_key(seed).hex()
    signature = sign_post(seed, "standup", "alpha", "ready to merge")

    assert verify_post(key, "standup", "alpha", "ready to merge", signature)
    # Every field is bound.
    assert not verify_post(key, "planning", "alpha", "ready to merge", signature)
    assert not verify_post(key, "standup", "beta", "ready to merge", signature)
    assert not verify_post(key, "standup", "alpha", "ready to ship", signature)


def test_garbage_hex_verifies_as_false():
    assert verify_post("zz", "b", "a", "x", "zz") is False
    assert verify_post("", "b", "a", "x", "") is False


def test_a_fingerprint_is_stable_and_survives_nonsense():
    key = ed25519.public_key(ed25519.generate_seed()).hex()
    assert fingerprint(key) == fingerprint(key)
    assert len(fingerprint(key)) == 16
    assert fingerprint("not hex") == ""


# ==========================================================================
# through the kernel
# ==========================================================================


def config(name: str, **caps):
    return load_config_data({"name": name, "caps": caps}, base_dir=Path("/tmp"))


def slot_labelled(kernel: CapKernel, actor: str, label: str) -> int:
    for info in kernel.cap_list(actor):
        if info.label == label:
            return info.slot
    raise AssertionError(f"{actor} holds no capability labelled {label!r}")


@pytest.fixture
def board_kernel():
    kernel = CapKernel()
    kernel.register_container(
        config("boss", factory={"rights": ["create"], "quota": {"containers": 1}})
    )
    seed = ed25519.generate_seed()
    kernel.find_container("boss").public_key = ed25519.public_key(seed).hex()
    slot = kernel.board_create(
        "boss", slot_labelled(kernel, "boss", "factory"), "standup"
    )["slot"]
    return kernel, seed, slot


def test_a_signed_post_is_stored_with_what_a_reader_needs(board_kernel):
    kernel, seed, slot = board_kernel
    signature = sign_post(seed, "standup", "boss", "ready")
    result = kernel.board_post("boss", slot, "ready", signature=signature)
    assert result["signed"] is True

    post = kernel.board_read("boss", slot)["posts"][0]
    assert post["signed"] is True
    # The key travels with the post, so verifying needs nothing else.
    assert verify_post(
        post["public_key"], "standup", post["from"], post["payload"], post["signature"]
    )


def test_a_signature_that_does_not_match_is_refused_not_stored(board_kernel):
    """Storing it unmarked would leave a post that looks signed and is not --
    worse than an unsigned post, and much worse than an error."""
    kernel, seed, slot = board_kernel
    wrong = sign_post(seed, "standup", "boss", "something else entirely")

    with pytest.raises(CapabilityError, match="does not match"):
        kernel.board_post("boss", slot, "ready", signature=wrong)
    assert kernel.board_read("boss", slot)["posts"] == []


def test_signing_as_someone_else_does_not_verify(board_kernel):
    """The author is taken from the socket, so a post signed as another
    container is signed over bytes that do not describe it."""
    kernel, seed, slot = board_kernel
    as_alpha = sign_post(seed, "standup", "alpha", "ready")
    with pytest.raises(CapabilityError):
        kernel.board_post("boss", slot, "ready", signature=as_alpha)


def test_a_container_with_no_key_cannot_sign(board_kernel):
    kernel, seed, slot = board_kernel
    kernel.find_container("boss").public_key = ""
    with pytest.raises(CapabilityError, match="no signing key"):
        kernel.board_post(
            "boss", slot, "ready", signature=sign_post(seed, "standup", "boss", "ready")
        )


def test_unsigned_posts_still_work_and_are_marked_as_such(board_kernel):
    """Signing is optional; a board that demanded it would be a different tool."""
    kernel, _seed, slot = board_kernel
    kernel.board_post("boss", slot, "just a note")
    post = kernel.board_read("boss", slot)["posts"][0]
    assert post["signed"] is False
    assert post["signature"] == ""


def test_a_tampered_post_stops_verifying(board_kernel):
    """The property the whole thing exists for: a reader can tell."""
    kernel, seed, slot = board_kernel
    kernel.board_post(
        "boss", slot, "ship it", signature=sign_post(seed, "standup", "boss", "ship it")
    )

    board = kernel.boards()[0]
    board.posts[0]["payload"] = "do not ship it"  # someone edits the record

    post = kernel.board_read("boss", slot)["posts"][0]
    assert not verify_post(
        post["public_key"], "standup", post["from"], post["payload"], post["signature"]
    )


# ==========================================================================
# signed messages
# ==========================================================================


class Recording:
    """Hooks that keep what was delivered, so the message can be inspected."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, dict]] = []

    def deliver_message(self, target, message):
        self.delivered.append((target, message))

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def messaging():
    kernel = CapKernel()
    kernel.hooks = Recording()
    for name in ("beta", "gamma"):
        kernel.register_container(config(name))
    kernel.register_container(
        config(
            "alpha",
            peers=[
                {"container": "beta", "rights": ["send"]},
                {"container": "gamma", "rights": ["send"]},
            ],
        )
    )
    seed = ed25519.generate_seed()
    kernel.find_container("alpha").public_key = ed25519.public_key(seed).hex()
    return kernel, seed


def test_a_signed_message_carries_what_the_recipient_needs(messaging):
    from capwrap.kernel.signing import sign_message, verify_message

    kernel, seed = messaging
    slot = slot_labelled(kernel, "alpha", "peer:beta")
    signature = sign_message(seed, "alpha", "bench is green")
    kernel.msg_send("alpha", slot, "bench is green", signature=signature)

    _target, message = kernel.hooks.delivered[0]
    assert verify_message(
        message["public_key"], message["from"], message["payload"], message["signature"]
    )


def test_one_signature_covers_a_whole_broadcast(messaging):
    """The signature binds the author and the payload, not the recipient --
    which is what lets a broadcast cost one signature instead of one each."""
    from capwrap.kernel.signing import sign_message, verify_message

    kernel, seed = messaging
    slots = [slot_labelled(kernel, "alpha", f"peer:{n}") for n in ("beta", "gamma")]
    signature = sign_message(seed, "alpha", "bench is green")
    result = kernel.msg_broadcast("alpha", slots, "bench is green", signature=signature)

    assert sorted(result["recipients"]) == ["beta", "gamma"]
    assert len(kernel.hooks.delivered) == 2
    for _target, message in kernel.hooks.delivered:
        assert verify_message(
            message["public_key"], "alpha", message["payload"], message["signature"]
        )


def test_a_forged_message_signature_is_refused_and_nothing_is_sent(messaging):
    kernel, _seed = messaging
    slot = slot_labelled(kernel, "alpha", "peer:beta")
    with pytest.raises(CapabilityError, match="does not match"):
        kernel.msg_send("alpha", slot, "trust me", signature="aa" * 64)
    assert kernel.hooks.delivered == []


def test_a_bad_signature_stops_a_broadcast_before_any_of_it_is_sent(messaging):
    """Checked once, up front: a partial broadcast of a message that turns out
    to be forged halfway through would be the worst outcome."""
    kernel, _seed = messaging
    slots = [slot_labelled(kernel, "alpha", f"peer:{n}") for n in ("beta", "gamma")]
    with pytest.raises(CapabilityError):
        kernel.msg_broadcast("alpha", slots, "trust me", signature="bb" * 64)
    assert kernel.hooks.delivered == []


def test_an_unsigned_message_still_goes(messaging):
    kernel, _seed = messaging
    slot = slot_labelled(kernel, "alpha", "peer:beta")
    kernel.msg_send("alpha", slot, "just a note")

    _target, message = kernel.hooks.delivered[0]
    assert message["signature"] == ""


def test_a_board_signature_is_not_a_message_signature(messaging):
    """Domain separation, checked. Without the tag, the same text signed for a
    board would verify as a direct message and the reverse."""
    from capwrap.kernel.signing import sign_post

    kernel, seed = messaging
    as_a_post = sign_post(seed, "standup", "alpha", "same words")
    slot = slot_labelled(kernel, "alpha", "peer:beta")

    with pytest.raises(CapabilityError):
        kernel.msg_send("alpha", slot, "same words", signature=as_a_post)


def test_a_forwarded_message_keeps_its_author(messaging):
    """The case the recipient is deliberately left out of the signature for.

    beta relays alpha's report to gamma. The kernel attributes the relayed
    message to beta, correctly -- beta sent it. The signature still says alpha
    wrote it, and gamma can check that without trusting beta.
    """
    from capwrap.kernel.signing import sign_message, verify_message

    kernel, seed = messaging
    signature = sign_message(seed, "alpha", "bench is green")
    kernel.msg_send(
        "alpha",
        slot_labelled(kernel, "alpha", "peer:beta"),
        "bench is green",
        signature=signature,
    )

    # beta passes it on verbatim.
    kernel.register_container(
        config("relay", peers=[{"container": "gamma", "rights": ["send"]}])
    )
    _target, original = kernel.hooks.delivered[0]
    kernel.msg_send(
        "relay", slot_labelled(kernel, "relay", "peer:gamma"), original["payload"]
    )

    _to, forwarded = kernel.hooks.delivered[-1]
    assert forwarded["from"] == "relay"  # who sent it
    assert verify_message(  # who wrote it
        original["public_key"], "alpha", forwarded["payload"], signature
    )
