# =============================================================================
# simulation/network.py — Controlled network fault injection.
#
# This module is the ONLY place where randomness is introduced.
# The server session imports these helpers and applies them before sending.
# Keeping simulation logic here means it can be disabled cleanly via config
# without touching any transfer logic.
# =============================================================================

import logging
import os
import random

import config
from core.protocol import Chunk

logger = logging.getLogger(__name__)


def maybe_drop(chunk: Chunk) -> bool:
    """Decide whether to silently drop this chunk.

    Args:
        chunk: The chunk about to be sent.

    Returns:
        ``True``  → drop the chunk (do not send it).
        ``False`` → send it normally.
    """
    if not config.SIMULATE_ERRORS:
        return False

    if random.random() < config.DROP_PROBABILITY:
        logger.debug(
            "SIMULATION — dropped chunk %d for client %d",
            chunk.seq_num,
            chunk.client_id,
        )
        return True

    return False


def maybe_corrupt(chunk: Chunk) -> Chunk:
    """Optionally corrupt a chunk's payload by flipping a random byte.

    Returns the original chunk untouched most of the time.
    When corruption is applied a *new* Chunk is returned so the original
    object (which the server may need to resend) is never mutated.

    Args:
        chunk: The chunk about to be sent.

    Returns:
        Either the original chunk or a new chunk with one byte flipped.
    """
    if not config.SIMULATE_ERRORS:
        return chunk

    if random.random() < config.CORRUPT_PROBABILITY:
        payload = bytearray(chunk.payload)
        if payload:                          # guard against empty payloads
            idx = random.randrange(len(payload))
            payload[idx] ^= 0xFF             # flip all bits in that byte
            logger.debug(
                "SIMULATION — corrupted byte %d in chunk %d for client %d",
                idx,
                chunk.seq_num,
                chunk.client_id,
            )
            return Chunk(
                client_id=chunk.client_id,
                seq_num=chunk.seq_num,
                total_chunks=chunk.total_chunks,
                is_last=chunk.is_last,
                payload=bytes(payload),
            )

    return chunk