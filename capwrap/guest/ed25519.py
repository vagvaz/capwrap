"""Ed25519, in pure Python, because both sides of the sandbox need it.

Signing board posts needs the same code in two places: the daemon, which
verifies, and `capctl` inside a container, which signs. `capctl` may not import
anything from capwrap and may not assume a package is installed -- the guest
tools directory is bind-mounted read-only into an otherwise unrelated
filesystem -- so a dependency like `cryptography` is available to the host and
not to the guest, which is the wrong way round.

So: one file, standard library only, imported by both. It is the RFC 8032
reference construction. Slow -- a few milliseconds per operation -- which is
irrelevant for signing a message somebody wrote and would be disqualifying for
anything on a hot path. Do not reach for this to protect a channel; the sandbox
boundary and the per-container socket do that. This exists so a post can be
checked *afterwards*, by someone who was not there and does not have to trust
the daemon that recorded it.

Verified against the RFC 8032 section 7.1 test vectors in the test suite.
"""

from __future__ import annotations

import hashlib
import os

# Curve25519 / edwards25519 parameters, RFC 8032 section 5.1.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)          # a square root of -1

#: Base point.
_BY = (4 * pow(5, _P - 2, _P)) % _P


def _x_recover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_B = (_x_recover(_BY) % _P, _BY % _P, 1, (_x_recover(_BY) * _BY) % _P)


def _add(p: tuple, q: tuple) -> tuple:
    """Extended-coordinate point addition; avoids an inversion per step."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult(p: tuple, e: int) -> tuple:
    if e == 0:
        return (0, 1, 1, 0)
    q = _scalarmult(p, e // 2)
    q = _add(q, q)
    if e & 1:
        q = _add(q, p)
    return q


def _encode_point(p: tuple) -> bytes:
    x, y, z, _t = p
    zi = pow(z, _P - 2, _P)
    x, y = (x * zi) % _P, (y * zi) % _P
    return ((y & ~(1 << 255)) | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(data: bytes) -> tuple | None:
    value = int.from_bytes(data, "little")
    y = value & ((1 << 255) - 1)
    if y >= _P:
        return None
    x = _x_recover(y)
    if x & 1 != (value >> 255) & 1:
        x = _P - x
    point = (x, y, 1, (x * y) % _P)
    return point if _on_curve(point) else None


def _on_curve(p: tuple) -> bool:
    x, y, z, t = p
    return (
        (x * y) % _P == (z * t) % _P
        and (-x * x + y * y - z * z - _D * t * t) % _P == 0
    )


def _hash_to_scalar(*chunks: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(chunks)).digest(), "little") % _L


def _clamp(digest: bytes) -> int:
    a = int.from_bytes(digest[:32], "little")
    a &= (1 << 254) - 8            # clear the low 3 bits and the top bit
    a |= 1 << 254                  # set the second-highest bit
    return a


# --------------------------------------------------------------------------
# the part anything outside this file uses
# --------------------------------------------------------------------------


def generate_seed() -> bytes:
    """A fresh 32-byte private seed."""
    return os.urandom(32)


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key for a seed."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    return _encode_point(_scalarmult(_B, _clamp(digest)))


def sign(seed: bytes, message: bytes) -> bytes:
    """A 64-byte signature over `message`."""
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    digest = hashlib.sha512(seed).digest()
    a = _clamp(digest)
    prefix = digest[32:]
    encoded_a = _encode_point(_scalarmult(_B, a))

    r = _hash_to_scalar(prefix, message)
    big_r = _encode_point(_scalarmult(_B, r))
    k = _hash_to_scalar(big_r, encoded_a, message)
    s = (r + k * a) % _L
    return big_r + s.to_bytes(32, "little")


def verify(key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is a valid signature over `message` by `key`.

    Returns False rather than raising for every kind of malformed input: a
    caller checking a signature wants one answer, and a bad signature and a
    corrupt one mean the same thing to it.
    """
    if len(key) != 32 or len(signature) != 64:
        return False
    try:
        big_r = _decode_point(signature[:32])
        point_a = _decode_point(key)
    except (ValueError, OverflowError):
        return False
    if big_r is None or point_a is None:
        return False

    s = int.from_bytes(signature[32:], "little")
    if s >= _L:                     # non-canonical S; reject rather than reduce
        return False

    k = _hash_to_scalar(signature[:32], key, message)
    left = _scalarmult(_B, s)
    right = _add(big_r, _scalarmult(point_a, k))
    return _encode_point(left) == _encode_point(right)


def fingerprint(key: bytes) -> str:
    """A short, readable name for a public key, for logs and the console."""
    return hashlib.sha256(key).hexdigest()[:16]
