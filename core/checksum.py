# =============================================================================
# core/checksum.py — SHA-256 checksum utilities.
#
# Kept in its own module so the algorithm can be swapped (e.g., BLAKE3)
# without touching any other file.
# =============================================================================

import hashlib


def compute(data: bytes) -> str:
    """Compute the SHA-256 digest of *data* as a hex string.

    Args:
        data: Arbitrary bytes (full file content or a single chunk).

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, expected_hex: str) -> bool:
    """Return True if SHA-256 of *data* matches *expected_hex*.

    Args:
        data:         Bytes to checksum.
        expected_hex: Hex digest previously produced by :func:`compute`.

    Returns:
        ``True`` if the digests match, ``False`` otherwise.
    """
    return compute(data) == expected_hex.lower()