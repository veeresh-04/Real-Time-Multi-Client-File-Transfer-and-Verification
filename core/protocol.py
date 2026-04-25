# =============================================================================
# core/protocol.py — Wire protocol for the file transfer system.
#
# Every byte that crosses the network is defined here.
#
# Chunk wire format (fixed-size header + variable payload):
#
#  0       4       8      12   13             13+N
#  +-------+-------+------+----+--------------+
#  |client |seq_   |total |last| payload      |
#  |_id    |num    |chunks|    | (≤CHUNK_SIZE)|
#  | (4B)  | (4B)  | (4B) |(1B)| (N bytes)    |
#  +-------+-------+------+----+--------------+
#
#  Total header = 13 bytes (network byte-order, big-endian)
#
# Message types sent as a single-byte opcode before the chunk header:
#   0x01  UPLOAD_REQUEST   client → server   "I want to send you a file"
#   0x02  CHUNK_DATA       server → client   "here is chunk N"
#   0x03  ACK              both directions   "chunk N received OK"
#   0x04  NACK             client → server   "chunk N failed, resend"
#   0x05  TRANSFER_DONE    server → client   "all chunks sent + checksum"
#   0x06  ERROR            both directions   "something went wrong"
# =============================================================================

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import ClassVar

import config


# ---------------------------------------------------------------------------
# Opcodes
# ---------------------------------------------------------------------------

class Opcode:
    """Single-byte message type identifiers."""

    UPLOAD_REQUEST: bytes = b"\x01"
    CHUNK_DATA: bytes     = b"\x02"
    ACK: bytes            = b"\x03"
    NACK: bytes           = b"\x04"
    TRANSFER_DONE: bytes  = b"\x05"
    ERROR: bytes          = b"\x06"


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """One unit of file data as it travels over the wire.

    Attributes:
        client_id:     Identifies which client session this chunk belongs to.
        seq_num:       Zero-based position of this chunk in the file.
        total_chunks:  Total number of chunks the file was split into.
        is_last:       True only for the final chunk of the file.
        payload:       Raw bytes for this chunk (≤ CHUNK_SIZE bytes).

    Wire header layout (17 bytes, network byte-order):
        4B  client_id     (uint32)
        4B  seq_num       (uint32)
        4B  total_chunks  (uint32)
        1B  is_last       (bool)
        4B  crc32         (uint32) — CRC32 of the payload for corruption detection
    """

    # Struct format — network byte order (!), unsigned ints, bool, crc32
    _STRUCT_FORMAT: ClassVar[str] = "!I I I ? I"
    _HEADER_SIZE: ClassVar[int] = struct.calcsize(_STRUCT_FORMAT)  # 17 bytes

    client_id: int
    seq_num: int
    total_chunks: int
    is_last: bool
    payload: bytes = field(repr=False)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialise the chunk to its wire representation.

        Returns:
            header (17 bytes) + payload concatenated.
        """
        import zlib
        crc = zlib.crc32(self.payload) & 0xFFFFFFFF
        header = struct.pack(
            self._STRUCT_FORMAT,
            self.client_id,
            self.seq_num,
            self.total_chunks,
            self.is_last,
            crc,
        )
        return header + self.payload

    @classmethod
    def from_bytes(cls, data: bytes) -> "Chunk":
        """Deserialise a chunk from raw bytes received off the wire.

        Args:
            data: Raw bytes starting at the header. Payload follows immediately.

        Returns:
            A fully populated Chunk instance.

        Raises:
            ValueError: If ``data`` is shorter than the fixed header.
        """
        if len(data) < cls._HEADER_SIZE:
            raise ValueError(
                f"Data too short for chunk header: got {len(data)} bytes, "
                f"need {cls._HEADER_SIZE}."
            )
        client_id, seq_num, total_chunks, is_last, crc = struct.unpack(
            cls._STRUCT_FORMAT, data[: cls._HEADER_SIZE]
        )
        payload = data[cls._HEADER_SIZE :]
        chunk = cls(
            client_id=client_id,
            seq_num=seq_num,
            total_chunks=total_chunks,
            is_last=bool(is_last),
            payload=payload,
        )
        chunk._expected_crc = crc   # stored for validation by the client
        return chunk

    def is_crc_valid(self) -> bool:
        """Return True if the payload CRC32 matches the header's recorded value."""
        import zlib
        expected = getattr(self, "_expected_crc", None)
        if expected is None:
            return True  # locally constructed chunk — no CRC to verify
        actual = zlib.crc32(self.payload) & 0xFFFFFFFF
        return actual == expected

    @property
    def header_size(self) -> int:
        return self._HEADER_SIZE


# ---------------------------------------------------------------------------
# ACK / NACK messages  (lightweight — just opcode + client_id + seq_num)
# ---------------------------------------------------------------------------

@dataclass
class AckMessage:
    """Acknowledgement: chunk received and accepted."""

    _STRUCT_FORMAT: ClassVar[str] = "!I I"   # client_id, seq_num
    _SIZE: ClassVar[int] = struct.calcsize(_STRUCT_FORMAT)  # 8 bytes

    client_id: int
    seq_num: int

    def to_bytes(self) -> bytes:
        return Opcode.ACK + struct.pack(self._STRUCT_FORMAT, self.client_id, self.seq_num)

    @classmethod
    def from_bytes(cls, data: bytes) -> "AckMessage":
        if len(data) < cls._SIZE:
            raise ValueError("ACK message too short.")
        client_id, seq_num = struct.unpack(cls._STRUCT_FORMAT, data[: cls._SIZE])
        return cls(client_id=client_id, seq_num=seq_num)


@dataclass
class NackMessage:
    """Negative acknowledgement: chunk lost or corrupted, please resend."""

    _STRUCT_FORMAT: ClassVar[str] = "!I I"
    _SIZE: ClassVar[int] = struct.calcsize(_STRUCT_FORMAT)

    client_id: int
    seq_num: int

    def to_bytes(self) -> bytes:
        return Opcode.NACK + struct.pack(self._STRUCT_FORMAT, self.client_id, self.seq_num)

    @classmethod
    def from_bytes(cls, data: bytes) -> "NackMessage":
        if len(data) < cls._SIZE:
            raise ValueError("NACK message too short.")
        client_id, seq_num = struct.unpack(cls._STRUCT_FORMAT, data[: cls._SIZE])
        return cls(client_id=client_id, seq_num=seq_num)


# ---------------------------------------------------------------------------
# Helpers — used by both server and client
# ---------------------------------------------------------------------------

def split_file_into_chunks(data: bytes, client_id: int) -> list[Chunk]:
    """Divide raw file bytes into a list of sequenced Chunk objects.

    Args:
        data:      Complete file contents as bytes.
        client_id: The session ID to stamp on every chunk.

    Returns:
        Ordered list of Chunk objects ready for transmission.
    """
    size = config.CHUNK_SIZE
    raw_chunks = [data[i : i + size] for i in range(0, max(len(data), 1), size)]
    total = len(raw_chunks)

    return [
        Chunk(
            client_id=client_id,
            seq_num=idx,
            total_chunks=total,
            is_last=(idx == total - 1),
            payload=piece,
        )
        for idx, piece in enumerate(raw_chunks)
    ]


def recv_exact(sock, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, blocking until available.

    Args:
        sock: A connected socket object.
        n:    Number of bytes to read.

    Returns:
        Exactly ``n`` bytes.

    Raises:
        ConnectionError: If the connection closes before ``n`` bytes arrive.
    """
    buf = bytearray()
    while len(buf) < n:
        packet = sock.recv(n - len(buf))
        if not packet:
            raise ConnectionError(
                f"Connection closed after {len(buf)} of {n} expected bytes."
            )
        buf.extend(packet)
    return bytes(buf)