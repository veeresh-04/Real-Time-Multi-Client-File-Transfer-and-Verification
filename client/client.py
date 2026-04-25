# =============================================================================
# client/client.py — Async file transfer client.
#
# Lifecycle per run():
#   1. Connect to server via TCP.
#   2. Upload local file  (UPLOAD_REQUEST).
#   3. Receive chunks back, ACK good / NACK bad or timed-out chunks.
#   4. Reassemble chunks in sequence order.
#   5. Receive TRANSFER_DONE + SHA-256 digest from server.
#   6. Verify digest against local reassembly.
#   7. Write verified file to CLIENT_OUTPUT_DIR.
#
# Wire format for CHUNK_DATA (server → client):
#   1B  — Opcode.CHUNK_DATA  (0x02)
#   2B  — payload length     (uint16, network order)
#   13B — chunk header       (client_id, seq_num, total_chunks, is_last)
#   NB  — payload            (N == payload length from above)
#
# Wire format for TRANSFER_DONE (server → client):
#   1B  — Opcode.TRANSFER_DONE  (0x05)
#   2B  — digest length          (uint16, always 64 for SHA-256)
#   64B — ASCII hex digest
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path

import config
from core import checksum
from core.errors import (
    ChecksumMismatchError,
    ClientDisconnectedError,
    MaxRetriesExceededError,
    ProtocolError,
)
from core.protocol import AckMessage, Chunk, NackMessage, Opcode

logger = logging.getLogger(__name__)


class FileTransferClient:
    """Uploads a file to the server and receives it back with integrity verification.

    Args:
        client_id: Integer label used for logging and output file naming.
        file_path: Path to the local file to upload.
    """

    def __init__(self, client_id: int, file_path: str | Path) -> None:
        self.client_id = client_id
        self.file_path = Path(file_path)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> bool:
        """Execute the full transfer lifecycle. Returns True on success.

        All domain exceptions are caught and logged here so that concurrent
        clients running in the same event loop are fully independent.
        """
        logger.info(
            "Client %d — starting transfer of '%s'",
            self.client_id, self.file_path.name,
        )
        try:
            await self._connect()
            file_data = self._read_local_file()
            await self._upload(file_data)
            received_data = await self._receive_chunks()
            server_digest = await self._receive_transfer_done()
            self._verify(received_data, server_digest)
            self._save_output(received_data)
            logger.info("Client %d — Transfer Successful ✓", self.client_id)
            return True

        except ChecksumMismatchError as exc:
            logger.error("Client %d — Integrity failure: %s", self.client_id, exc)
        except MaxRetriesExceededError as exc:
            logger.error("Client %d — Retry limit hit: %s", self.client_id, exc)
        except ProtocolError as exc:
            logger.error("Client %d — Protocol error: %s", self.client_id, exc)
        except ClientDisconnectedError as exc:
            logger.error("Client %d — Server disconnected: %s", self.client_id, exc)
        except FileNotFoundError:
            logger.error(
                "Client %d — File not found: '%s'", self.client_id, self.file_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Client %d — Unexpected error: %s", self.client_id, exc)
        finally:
            await self._close()

        return False

    # ------------------------------------------------------------------
    # Phase 0 — Connect
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Open a TCP connection to the server."""
        self._reader, self._writer = await asyncio.open_connection(
            config.HOST, config.PORT
        )
        logger.debug(
            "Client %d — connected to %s:%d", self.client_id, config.HOST, config.PORT
        )

    # ------------------------------------------------------------------
    # Phase 1 — Read local file and upload
    # ------------------------------------------------------------------

    def _read_local_file(self) -> bytes:
        """Read the entire local file into memory.

        Raises:
            FileNotFoundError: If self.file_path does not exist.
        """
        data = self.file_path.read_bytes()
        logger.info(
            "Client %d — read %d bytes from '%s'.",
            self.client_id, len(data), self.file_path.name,
        )
        return data

    async def _upload(self, file_data: bytes) -> None:
        """Send UPLOAD_REQUEST to the server.

        Wire format:
            1B  — Opcode.UPLOAD_REQUEST  (0x01)
            4B  — file size              (uint32, network order)
            NB  — raw file bytes
        """
        size_header = struct.pack("!I", len(file_data))
        await self._write(Opcode.UPLOAD_REQUEST + size_header + file_data)
        logger.info(
            "Client %d — upload sent (%d bytes).", self.client_id, len(file_data)
        )

    # ------------------------------------------------------------------
    # Phase 2 — Receive chunks, ACK / NACK each one
    # ------------------------------------------------------------------

    async def _receive_chunks(self) -> bytes:
        """Drive the chunk-receive loop until TRANSFER_DONE arrives.

        For each incoming CHUNK_DATA:
          - Read it off the wire.
          - Validate it (non-empty payload as a basic corruption check).
          - ACK if valid; NACK if corrupt (server will retransmit).
          - Track per-chunk NACK count; raise after MAX_RETRIES.

        A dropped chunk is detected when _read_opcode_with_timeout returns
        None.  The client then sends a NACK with seq_num = -1 to signal
        "I'm still waiting" and loops again.

        Returns:
            Fully reassembled and ordered file bytes.

        Raises:
            MaxRetriesExceededError: If any chunk cannot be received cleanly.
            ProtocolError:           On unexpected opcodes or missing chunks.
            ClientDisconnectedError: On server disconnect.
        """
        received: dict[int, bytes] = {}     # seq_num → payload
        total_chunks: int | None = None
        nack_counts: dict[int, int] = {}    # seq_num → consecutive NACKs

        while True:
            opcode = await self._read_opcode_with_timeout()

            # ── CHUNK_DATA ───────────────────────────────────────────
            if opcode == Opcode.CHUNK_DATA:
                chunk = await self._read_chunk_body()

                if total_chunks is None:
                    total_chunks = chunk.total_chunks

                if self._is_valid(chunk):
                    received[chunk.seq_num] = chunk.payload
                    nack_counts.pop(chunk.seq_num, None)   # reset on success
                    await self._send_ack(chunk.seq_num)
                    logger.debug(
                        "Client %d — ACK chunk %d / %d",
                        self.client_id, chunk.seq_num + 1, total_chunks,
                    )
                else:
                    count = nack_counts.get(chunk.seq_num, 0) + 1
                    nack_counts[chunk.seq_num] = count
                    if count > config.MAX_RETRIES:
                        raise MaxRetriesExceededError(chunk.seq_num, count)
                    await self._send_nack(chunk.seq_num)
                    logger.warning(
                        "Client %d — NACK chunk %d (attempt %d / %d)",
                        self.client_id, chunk.seq_num, count, config.MAX_RETRIES,
                    )

            # ── TIMEOUT (dropped chunk) ───────────────────────────────
            elif opcode is None:
                # Server dropped a chunk.  NACK with seq_num = 0xFFFFFFFF
                # (max uint32) signals "I'm still waiting — resend pending chunks."
                await self._send_nack(0xFFFFFFFF)
                logger.warning(
                    "Client %d — timeout waiting for chunk, sent NACK.", self.client_id
                )

            # ── TRANSFER_DONE ─────────────────────────────────────────
            elif opcode == Opcode.TRANSFER_DONE:
                break   # digest is read in _receive_transfer_done()

            # ── ERROR from server ─────────────────────────────────────
            elif opcode == Opcode.ERROR:
                msg = await self._read_error_body()
                raise ProtocolError(f"Server error: {msg}")

            else:
                raise ProtocolError(f"Unexpected opcode 0x{opcode.hex()}")

        if total_chunks is None:
            raise ProtocolError(
                "No chunks received — server sent TRANSFER_DONE immediately."
            )

        return self._reassemble(received, total_chunks)

    async def _read_opcode_with_timeout(self) -> bytes | None:
        """Read 1-byte opcode, returning None on timeout (indicates dropped chunk)."""
        try:
            return await asyncio.wait_for(
                self._read_exact(1), timeout=config.RETRY_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None

    async def _read_chunk_body(self) -> Chunk:
        """Read payload-length prefix, then full chunk header + payload.

        Wire layout (after the CHUNK_DATA opcode byte):
            2B  — payload length  (uint16, network order)
            13B — chunk header    (client_id, seq_num, total_chunks, is_last)
            NB  — payload
        """
        raw_len = await self._read_exact(2)
        (payload_len,) = struct.unpack("!H", raw_len)

        header_bytes = await self._read_exact(Chunk._HEADER_SIZE)
        payload = await self._read_exact(payload_len)

        return Chunk.from_bytes(header_bytes + payload)

    @staticmethod
    def _is_valid(chunk: Chunk) -> bool:
        """Return False if the chunk fails its CRC32 integrity check.

        Every chunk carries a CRC32 of its payload in the header (computed
        by the server before any simulation faults are applied).  The client
        recomputes and compares — a mismatch means the payload was corrupted
        in transit and the chunk must be NACKed for retransmission.

        An empty payload on the sole chunk of an empty file is always valid.
        """
        if chunk.is_last and chunk.total_chunks == 1 and len(chunk.payload) == 0:
            return True   # legitimate empty-file chunk
        return chunk.is_crc_valid()

    def _reassemble(self, received: dict[int, bytes], total: int) -> bytes:
        """Concatenate payloads in seq_num order.

        Raises:
            ProtocolError: If any chunk seq_num is absent from *received*.
        """
        missing = [i for i in range(total) if i not in received]
        if missing:
            raise ProtocolError(
                f"Client {self.client_id} — {len(missing)} missing chunk(s): "
                f"seq_nums {missing[:10]}"
            )
        return b"".join(received[i] for i in range(total))

    # ------------------------------------------------------------------
    # Phase 3 — Receive TRANSFER_DONE + digest
    # ------------------------------------------------------------------

    async def _receive_transfer_done(self) -> str:
        """Read the SHA-256 digest that follows the TRANSFER_DONE opcode.

        The opcode byte was already consumed in _receive_chunks.
        Remaining wire layout:
            2B  — digest length  (uint16, always 64 for SHA-256)
            64B — ASCII hex digest

        Returns:
            64-character lowercase hex string.
        """
        raw_len = await self._read_exact(2)
        (digest_len,) = struct.unpack("!H", raw_len)
        digest = (await self._read_exact(digest_len)).decode("ascii")
        logger.debug(
            "Client %d — received server checksum: %s…",
            self.client_id, digest[:16],
        )
        return digest

    # ------------------------------------------------------------------
    # Phase 4 — Verify integrity and persist
    # ------------------------------------------------------------------

    def _verify(self, data: bytes, expected: str) -> None:
        """Recompute SHA-256 and compare to server's digest.

        Raises:
            ChecksumMismatchError: If the digests differ.
        """
        if not checksum.verify(data, expected):
            raise ChecksumMismatchError(
                expected=expected, actual=checksum.compute(data)
            )
        logger.info("Client %d — checksum verified ✓", self.client_id)

    def _save_output(self, data: bytes) -> None:
        """Write the reassembled file to CLIENT_OUTPUT_DIR."""
        out_dir = Path(config.CLIENT_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"client_{self.client_id}_{self.file_path.name}"
        dest.write_bytes(data)
        logger.info("Client %d — saved to '%s'", self.client_id, dest)

    # ------------------------------------------------------------------
    # ACK / NACK helpers
    # ------------------------------------------------------------------

    async def _send_ack(self, seq_num: int) -> None:
        await self._write(AckMessage(self.client_id, seq_num).to_bytes())

    async def _send_nack(self, seq_num: int) -> None:
        await self._write(NackMessage(self.client_id, seq_num).to_bytes())

    # ------------------------------------------------------------------
    # Low-level I/O helpers
    # ------------------------------------------------------------------

    async def _read_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes; raise ClientDisconnectedError on EOF."""
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ClientDisconnectedError(self.client_id) from exc

    async def _read_error_body(self) -> str:
        """Read the message body of an ERROR opcode (1B length + N bytes)."""
        (length,) = struct.unpack("!B", await self._read_exact(1))
        return (await self._read_exact(length)).decode("utf-8", errors="replace")

    async def _write(self, data: bytes) -> None:
        """Write bytes to the server stream and flush."""
        self._writer.write(data)
        await self._writer.drain()

    async def _close(self) -> None:
        """Gracefully close the TCP connection."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass