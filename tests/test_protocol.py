# =============================================================================
# tests/test_protocol.py — Unit tests for core/protocol.py and core/checksum.py
# =============================================================================

import struct
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
from core.protocol import (
    Chunk, AckMessage, NackMessage, Opcode,
    split_file_into_chunks, recv_exact,
)
from core import checksum
from core.errors import ChecksumMismatchError


# ---------------------------------------------------------------------------
# Chunk serialisation
# ---------------------------------------------------------------------------

class TestChunkSerialization:

    def test_round_trip_normal_chunk(self):
        chunk = Chunk(client_id=1, seq_num=0, total_chunks=3, is_last=False, payload=b"hello")
        restored = Chunk.from_bytes(chunk.to_bytes())
        assert restored.client_id    == chunk.client_id
        assert restored.seq_num      == chunk.seq_num
        assert restored.total_chunks == chunk.total_chunks
        assert restored.is_last      == chunk.is_last
        assert restored.payload      == chunk.payload

    def test_round_trip_last_chunk(self):
        chunk = Chunk(client_id=99, seq_num=4, total_chunks=5, is_last=True, payload=b"\x00\xff\xab")
        assert Chunk.from_bytes(chunk.to_bytes()).is_last is True

    def test_empty_payload_is_preserved(self):
        chunk = Chunk(client_id=1, seq_num=0, total_chunks=1, is_last=True, payload=b"")
        assert Chunk.from_bytes(chunk.to_bytes()).payload == b""

    def test_from_bytes_raises_on_short_data(self):
        with pytest.raises(ValueError, match="too short"):
            Chunk.from_bytes(b"\x00" * 5)   # header needs 13 bytes

    def test_header_size_constant_matches_struct(self):
        assert Chunk._HEADER_SIZE == struct.calcsize(Chunk._STRUCT_FORMAT)


# ---------------------------------------------------------------------------
# split_file_into_chunks
# ---------------------------------------------------------------------------

class TestSplitFileIntoChunks:

    def test_exact_multiple_of_chunk_size(self):
        data = b"X" * (config.CHUNK_SIZE * 2)   # exactly 2 chunks
        chunks = split_file_into_chunks(data, client_id=1)
        assert len(chunks) == 2
        assert chunks[-1].is_last is True
        assert all(not c.is_last for c in chunks[:-1])

    def test_non_multiple_creates_partial_last_chunk(self):
        data = b"A" * (config.CHUNK_SIZE + 476)  # 1 full chunk + 476 byte remainder
        chunks = split_file_into_chunks(data, client_id=2)
        assert len(chunks) == 2
        assert len(chunks[1].payload) == 476

    def test_empty_file_yields_one_chunk(self):
        chunks = split_file_into_chunks(b"", client_id=3)
        assert len(chunks) == 1
        assert chunks[0].payload == b""
        assert chunks[0].is_last is True

    def test_single_byte_file(self):
        chunks = split_file_into_chunks(b"\x42", client_id=4)
        assert len(chunks) == 1
        assert chunks[0].payload == b"\x42"

    def test_total_chunks_field_is_consistent(self):
        data = b"B" * 3000
        chunks = split_file_into_chunks(data, client_id=5)
        total = len(chunks)
        assert all(c.total_chunks == total for c in chunks)

    def test_seq_nums_are_zero_based_and_contiguous(self):
        chunks = split_file_into_chunks(b"C" * 5000, client_id=6)
        assert [c.seq_num for c in chunks] == list(range(len(chunks)))

    def test_reassembly_recovers_original_data(self):
        data = b"Hello, world! " * 200
        chunks = split_file_into_chunks(data, client_id=7)
        recovered = b"".join(c.payload for c in chunks)
        assert recovered == data

    def test_client_id_stamped_on_all_chunks(self):
        chunks = split_file_into_chunks(b"D" * 3000, client_id=42)
        assert all(c.client_id == 42 for c in chunks)


# ---------------------------------------------------------------------------
# AckMessage / NackMessage
# ---------------------------------------------------------------------------

class TestAckNack:

    def test_ack_opcode_prefix(self):
        assert AckMessage(1, 0).to_bytes()[0:1] == Opcode.ACK

    def test_nack_opcode_prefix(self):
        assert NackMessage(1, 0).to_bytes()[0:1] == Opcode.NACK

    def test_ack_round_trip(self):
        ack = AckMessage(client_id=7, seq_num=3)
        wire = ack.to_bytes()
        # strip the 1-byte opcode before from_bytes
        restored = AckMessage.from_bytes(wire[1:])
        assert restored.client_id == 7
        assert restored.seq_num   == 3

    def test_nack_round_trip(self):
        nack = NackMessage(client_id=12, seq_num=99)
        wire = nack.to_bytes()
        restored = NackMessage.from_bytes(wire[1:])
        assert restored.client_id == 12
        assert restored.seq_num   == 99


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------

class TestChecksum:

    def test_known_digest(self):
        # echo -n "" | sha256sum  → e3b0c44298fc1c149...
        assert checksum.compute(b"").startswith("e3b0c44298fc1c149")

    def test_verify_passes_on_correct_digest(self):
        data = b"file transfer test"
        assert checksum.verify(data, checksum.compute(data)) is True

    def test_verify_fails_on_tampered_data(self):
        data = b"original"
        digest = checksum.compute(data)
        assert checksum.verify(b"tampered", digest) is False

    def test_verify_case_insensitive(self):
        data = b"abc"
        digest = checksum.compute(data).upper()
        assert checksum.verify(data, digest) is True

    def test_checksum_mismatch_error_carries_both_digests(self):
        exc = ChecksumMismatchError(expected="aabb", actual="ccdd")
        assert exc.expected == "aabb"
        assert exc.actual   == "ccdd"
        assert "aabb" in str(exc)