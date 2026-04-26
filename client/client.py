# =============================================================================
# client/client.py — Async file transfer client.
#
# Protocol design — seq_num-driven delivery:
#
#   For each incoming CHUNK_DATA the client:
#     1. Reads the chunk off the wire.
#     2. Validates it with CRC32.
#     3. If valid  → stores payload, sends ACK(seq_num=chunk.seq_num).
#     4. If invalid → sends NACK(seq_num=chunk.seq_num), server retransmits.
#     5. On timeout → sends NACK(seq_num=0xFFFFFFFF), server retransmits
#        the current chunk.
#
#   Every ACK/NACK carries an explicit seq_num so the server always knows
#   exactly which chunk is being acknowledged, regardless of timing.
#   This eliminates the sync-drift bug that caused missing chunks under load.
#
# Wire format for CHUNK_DATA (server → client):
#   1B  Opcode.CHUNK_DATA
#   4B  payload length  (uint32), network order)
#   17B chunk header       (client_id, seq_num, total_chunks, is_last, crc32)
#   NB  payload
#
# Wire format for TRANSFER_DONE (server → client):
#   1B  Opcode.TRANSFER_DONE
#   2B  digest length       (uint16, always 64)
#   64B ASCII hex digest
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
        """Execute the full transfer lifecycle. Returns True on success."""
        logger.info(
            "Client %d — starting transfer of '%s'",
            self.client_id, self.file_path.name,
        )
        try:
            await self._connect()
            file_data     = self._read_local_file()
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
        self._reader, self._writer = await asyncio.open_connection(
            config.HOST, config.PORT
        )
        logger.debug(
            "Client %d — connected to %s:%d", self.client_id, config.HOST, config.PORT
        )

    # ------------------------------------------------------------------
    # Phase 1 — Upload
    # ------------------------------------------------------------------

    def _read_local_file(self) -> bytes:
        data = self.file_path.read_bytes()
        logger.info(
            "Client %d — read %d bytes from '%s'.",
            self.client_id, len(data), self.file_path.name,
        )
        return data

    async def _upload(self, file_data: bytes) -> None:
        """Send UPLOAD_REQUEST.

        Wire format:
            1B  Opcode.UPLOAD_REQUEST
            4B  file size (uint32, network order)
            NB  raw file bytes
        """
        await self._write(
            Opcode.UPLOAD_REQUEST
            + struct.pack("!I", len(file_data))
            + file_data
        )
        logger.info(
            "Client %d — upload sent (%d bytes).", self.client_id, len(file_data)
        )

    # ------------------------------------------------------------------
    # Phase 2 — Receive chunks
    # ------------------------------------------------------------------

    async def _receive_chunks(self) -> bytes:
        """Receive chunks, ACK or NACK each one by explicit seq_num.

        Key design:
            Every ACK and NACK carries chunk.seq_num so the server knows
            exactly which chunk is being acknowledged.  Timeout NACKs use
            sentinel 0xFFFFFFFF meaning "current chunk timed out, resend".

        Returns:
            Fully reassembled file bytes in correct order.
        """
        received: dict[int, bytes] = {}   # seq_num → payload
        total_chunks: int | None   = None
        nack_counts:  dict[int, int] = {} # seq_num → consecutive NACKs

        while True:
            opcode = await self._read_opcode_with_timeout()

            # ── CHUNK_DATA ────────────────────────────────────────────
            if opcode == Opcode.CHUNK_DATA:
                chunk = await self._read_chunk_body()

                if total_chunks is None:
                    total_chunks = chunk.total_chunks

                if self._is_valid(chunk):
                    received[chunk.seq_num] = chunk.payload
                    nack_counts.pop(chunk.seq_num, None)
                    await self._send_ack(chunk.seq_num)   # ← ACK carries seq_num
                    logger.debug(
                        "Client %d — ACK chunk %d / %d",
                        self.client_id, chunk.seq_num + 1, total_chunks,
                    )
                else:
                    count = nack_counts.get(chunk.seq_num, 0) + 1
                    nack_counts[chunk.seq_num] = count
                    if count > config.MAX_RETRIES:
                        raise MaxRetriesExceededError(chunk.seq_num, count)
                    await self._send_nack(chunk.seq_num)  # ← NACK carries seq_num
                    logger.warning(
                        "Client %d — NACK chunk %d (attempt %d / %d)",
                        self.client_id, chunk.seq_num, count, config.MAX_RETRIES,
                    )

            # ── TIMEOUT (dropped chunk) ───────────────────────────────
            elif opcode is None:
                # 0xFFFFFFFF = sentinel meaning "resend current chunk"
                await self._send_nack(0xFFFFFFFF)
                logger.warning(
                    "Client %d — timeout waiting for chunk, sent NACK.",
                    self.client_id,
                )

            # ── TRANSFER_DONE ─────────────────────────────────────────
            elif opcode == Opcode.TRANSFER_DONE:
                break

            # ── ERROR ─────────────────────────────────────────────────
            elif opcode == Opcode.ERROR:
                msg = await self._read_error_body()
                raise ProtocolError(f"Server error: {msg}")

            else:
                raise ProtocolError(f"Unexpected opcode 0x{opcode.hex()}")

        if total_chunks is None:
            raise ProtocolError("No chunks received — server sent TRANSFER_DONE immediately.")

        return self._reassemble(received, total_chunks)

    async def _read_opcode_with_timeout(self) -> bytes | None:
        """Read 1-byte opcode; return None on timeout (dropped chunk)."""
        try:
            return await asyncio.wait_for(
                self._read_exact(1), timeout=config.RETRY_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None

    async def _read_chunk_body(self) -> Chunk:
        """Read 2B payload-length, then 17B header, then payload.

        Wire layout after opcode:
            4B  payload length  (uint32))
            17B chunk header
            NB  payload
        """
        (payload_len,) = struct.unpack("!I", await self._read_exact(4))
        header_bytes   = await self._read_exact(Chunk._HEADER_SIZE)
        payload        = await self._read_exact(payload_len)
        return Chunk.from_bytes(header_bytes + payload)

    @staticmethod
    def _is_valid(chunk: Chunk) -> bool:
        """CRC32 integrity check.  Empty payload on empty-file chunk is valid."""
        if chunk.is_last and chunk.total_chunks == 1 and len(chunk.payload) == 0:
            return True
        return chunk.is_crc_valid()

    def _reassemble(self, received: dict[int, bytes], total: int) -> bytes:
        missing = [i for i in range(total) if i not in received]
        if missing:
            raise ProtocolError(
                f"Client {self.client_id} — {len(missing)} missing chunk(s): "
                f"seq_nums {missing[:10]}"
            )
        return b"".join(received[i] for i in range(total))

    # ------------------------------------------------------------------
    # Phase 3 — TRANSFER_DONE + checksum
    # ------------------------------------------------------------------

    async def _receive_transfer_done(self) -> str:
        """Read SHA-256 digest.  Opcode was already consumed in _receive_chunks.

        Wire layout:
            2B  digest length (uint16, always 64)
            64B ASCII hex digest
        """
        (digest_len,) = struct.unpack("!H", await self._read_exact(2))
        digest = (await self._read_exact(digest_len)).decode("ascii")
        logger.debug(
            "Client %d — received server checksum: %s…", self.client_id, digest[:16]
        )
        return digest

    # ------------------------------------------------------------------
    # Phase 4 — Verify + save
    # ------------------------------------------------------------------

    def _verify(self, data: bytes, expected: str) -> None:
        if not checksum.verify(data, expected):
            raise ChecksumMismatchError(expected=expected, actual=checksum.compute(data))
        logger.info("Client %d — checksum verified ✓", self.client_id)

    def _save_output(self, data: bytes) -> None:
        out_dir = Path(config.CLIENT_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"client_{self.client_id}_{self.file_path.name}"
        dest.write_bytes(data)
        logger.info("Client %d — saved to '%s'", self.client_id, dest)

    # ------------------------------------------------------------------
    # ACK / NACK senders
    # ------------------------------------------------------------------

    async def _send_ack(self, seq_num: int) -> None:
        await self._write(AckMessage(self.client_id, seq_num).to_bytes())

    async def _send_nack(self, seq_num: int) -> None:
        await self._write(NackMessage(self.client_id, seq_num).to_bytes())

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    async def _read_exact(self, n: int) -> bytes:
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ClientDisconnectedError(self.client_id) from exc

    async def _read_error_body(self) -> str:
        (length,) = struct.unpack("!B", await self._read_exact(1))
        return (await self._read_exact(length)).decode("utf-8", errors="replace")

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def _close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass