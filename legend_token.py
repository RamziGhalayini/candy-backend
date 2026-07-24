"""Legend Mode recovery-code generation -- deterministic HMAC-SHA256 of
(device_id + transaction_id), keyed by a server-side pepper, mapped through
an ambiguity-free Base32 alphabet (no 0/1/O/I). Deterministic, not random,
which is what makes mint's existing "mint again -> same code back" behavior
possible WITHOUT ever storing or retrieving the raw code: a repeat mint for
an already-known (device_id, transaction_id) pair just recomputes the exact
same code from those two values plus the pepper. Only the code's SHA-256
hash is ever persisted (see LegendUnlock.code_hash in app.py).
"""

import hashlib
import hmac
import os

# Same alphabet app/legend-mode.tsx's RECOVERY_CODE_ALPHABET client-side
# mask will need to move to once this scheme actually ships (that file's
# mask currently matches the OLD hex format on purpose -- see its own
# comment -- and must not be changed until this backend change is live,
# or it would start rejecting today's real codes).
_BASE32_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_HMAC_BYTES_USED = 5  # 5 bytes = 40 bits = 8 * 5-bit Base32 chars, no padding
_CODE_BODY_LENGTH = 8  # keeps the existing "LM-XXXXXXXX" shape unchanged


class LegendTokenConfigError(RuntimeError):
    """Raised when LEGEND_CODE_PEPPER isn't set yet."""


def _pepper() -> bytes:
    pepper = os.environ.get("LEGEND_CODE_PEPPER")
    if not pepper:
        raise LegendTokenConfigError("Missing required env var: LEGEND_CODE_PEPPER")
    return pepper.encode()


def generate_legend_code(device_id: str, transaction_id: str) -> str:
    """Deterministic: the same (device_id, transaction_id) under the same
    pepper always produces the same code."""
    payload = f"{device_id}:{transaction_id}".encode()
    digest = hmac.new(_pepper(), payload, hashlib.sha256).digest()
    return "LM-" + _encode_base32(digest[:_HMAC_BYTES_USED])


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _encode_base32(data: bytes) -> str:
    """Packs exactly _HMAC_BYTES_USED bytes into exactly _CODE_BODY_LENGTH
    characters of _BASE32_ALPHABET -- plain 5-bit-group bit-packing, NOT
    RFC 4648 Base32 (different alphabet, different padding rules this
    project doesn't need since the input length here is always fixed)."""
    if len(data) != _HMAC_BYTES_USED:
        raise ValueError(f"expected exactly {_HMAC_BYTES_USED} bytes, got {len(data)}")
    bits = int.from_bytes(data, "big")
    chars = []
    for i in range(_CODE_BODY_LENGTH):
        shift = (_CODE_BODY_LENGTH - 1 - i) * 5
        index = (bits >> shift) & 0b11111
        chars.append(_BASE32_ALPHABET[index])
    return "".join(chars)
