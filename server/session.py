# =============================================================================
# server/session.py — Async per-client file transfer session.
#
# Protocol design — seq_num-driven delivery:
#
#   For each chunk N the server:
#     1. Sends chunk N (with simulated faults applied).
#     2. Reads the client's response — an ACK or NACK carrying an explicit
#        seq_num field.
#     3. If the response seq_num matches N → move to N+1.
#     4. If NACK or timeout → retransmit N (always the clean original).
#     5. After MAX_RETRIES failures → abort with MaxRetriesExceededError.
#
#   The seq_num in every ACK/NACK is authoritative.  The server never
#   advances past a chunk until it receives an ACK whose seq_num == that
#   chunk's seq_num.  This eliminates the sync-drift bug that caused
#   missing chunks under load.
#
# Wire format per chunk (server → client):
#   1B  Opcode.CHUNK_DATA
#   4B  payload length  (uint32, network order))
#   17B chunk header    (client_id, seq_num, total_chunks, is_last, crc32)
#   NB  payload
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path

import config
from core import checksum
from core.errors import ClientDisconnectedError, MaxRetriesExceededError, ProtocolError
from core.protocol import AckMessage, Chunk, NackMessage, Opcode, split_file_into_chunks
from simulation.network import maybe_corrupt, maybe_drop

logger = logging.getLogger(__name__)


class ClientSession:
    """Manages the full file-transfer lifecycle for one connected client.

    Args:
        client_id: Unique integer assigned by the server at accept time.
        reader:    AsyncIO stream to read bytes FROM the client.
        writer:    AsyncIO stream to write bytes TO the client.
    """

    def __init__(
        self,
        client_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.client_id = client_id
        self._reader   = reader
        self._writer   = writer
        self._peer     = writer.get_extra_info("peername")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drive the session from upload through delivery."""
        logger.info("Session %d started — peer %s", self.client_id, self._peer)
        try:
            file_data = await self._receive_upload()
            chunks    = split_file_into_chunks(file_data, self.client_id)
            logger.info(
                "Session %d — split into %d chunk(s).", self.client_id, len(chunks)
            )
            await self._deliver_all_chunks(chunks)
            await self._send_transfer_done(file_data)
            logger.info("Session %d completed successfully.", self.client_id)
        except (ClientDisconnectedError, MaxRetriesExceededError, ProtocolError) as exc:
            logger.error("Session %d failed: %s", self.client_id, exc)
            await self._send_error(str(exc))
        finally:
            await self._close()

    # ------------------------------------------------------------------
    # Phase 1 — Receive upload
    # ------------------------------------------------------------------

    async def _receive_upload(self) -> bytes:
        """Read UPLOAD_REQUEST and return raw file bytes.

        Wire format:
            1B  Opcode.UPLOAD_REQUEST
            4B  file size (uint32, network order)
            NB  raw file data
        """
        opcode = await self._read_exact(1)
        if opcode != Opcode.UPLOAD_REQUEST:
            raise ProtocolError(
                f"Expected UPLOAD_REQUEST, got 0x{opcode.hex()} "
                f"from client {self.client_id}."
            )

        (file_size,) = struct.unpack("!I", await self._read_exact(4))
        logger.info(
            "Session %d — receiving upload: %d bytes", self.client_id, file_size
        )

        file_data = await self._read_exact(file_size)

        # Persist to disk (non-blocking)
        storage = Path(config.SERVER_STORAGE_DIR)
        storage.mkdir(parents=True, exist_ok=True)
        dest = storage / f"client_{self.client_id}.bin"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dest.write_bytes, file_data)

        return file_data

    # ------------------------------------------------------------------
    # Phase 2 — Deliver chunks
    # ------------------------------------------------------------------

    async def _deliver_all_chunks(self, chunks: list[Chunk]) -> None:
        """Send every chunk in order, retrying on NACK or timeout.

        Key design guarantee:
            The server does not advance to chunk N+1 until it receives
            an ACK with seq_num == N.  If it receives a NACK or a timeout
            it retransmits chunk N (always the clean original).
        """
        for chunk in chunks:
            await self._deliver_one_chunk(chunk)

    async def _deliver_one_chunk(self, chunk: Chunk) -> None:
        """Deliver a single chunk, retrying up to MAX_RETRIES times.

        Args:
            chunk: The clean original chunk.  Never mutated here.

        Raises:
            MaxRetriesExceededError: All retries exhausted.
        """
        for attempt in range(1, config.MAX_RETRIES + 1):

            # --- apply simulation faults (drop / corrupt) ---
            if maybe_drop(chunk):
                logger.debug(
                    "Session %d — chunk %d dropped (attempt %d).",
                    self.client_id, chunk.seq_num, attempt,
                )
                # Don't send anything — client will time out and NACK.
            else:
                await self._send_chunk(chunk)

            # --- wait for ACK or NACK carrying the explicit seq_num ---
            response = await self._read_response()

            if isinstance(response, AckMessage):
                if response.seq_num == chunk.seq_num:
                    # Correct ACK — chunk delivered.
                    logger.debug(
                        "Session %d — chunk %d ACK'd.", self.client_id, chunk.seq_num
                    )
                    return
                # ACK for wrong seq_num — client is confused; retransmit.
                logger.warning(
                    "Session %d — got ACK for seq %d, expected %d. Retransmitting.",
                    self.client_id, response.seq_num, chunk.seq_num,
                )
                continue

            if isinstance(response, NackMessage):
                logger.warning(
                    "Session %d — chunk %d NACK'd (attempt %d/%d), retransmitting.",
                    self.client_id, chunk.seq_num, attempt, config.MAX_RETRIES,
                )
                continue

        raise MaxRetriesExceededError(chunk.seq_num, config.MAX_RETRIES)

    async def _send_chunk(self, chunk: Chunk) -> None:
        """Serialise and transmit one chunk with simulated corruption.

        The CRC in the header is always computed from the CLEAN payload so
        that the client can detect corruption by comparing the received
        payload's CRC against the header value.

        Wire layout:
            1B  Opcode.CHUNK_DATA
            2B  payload length
            17B chunk header  (includes CRC of clean payload)
            NB  payload       (may be corrupted by simulation)
        """
        corrupted = maybe_corrupt(chunk)

        # Build header with CRC of the CLEAN payload.
        clean_wire = chunk.to_bytes()          # CRC computed here from clean payload

        if corrupted.payload != chunk.payload:
            # Splice corrupted payload after the clean header so client
            # detects the mismatch via CRC.
            wire = clean_wire[: chunk._HEADER_SIZE] + corrupted.payload
            payload_len = len(corrupted.payload)
        else:
            wire = clean_wire
            payload_len = len(chunk.payload)

        await self._write(
            Opcode.CHUNK_DATA + struct.pack("!I", payload_len) + wire
        )

    # ------------------------------------------------------------------
    # Phase 3 — Transfer done + checksum
    # ------------------------------------------------------------------

    async def _send_transfer_done(self, file_data: bytes) -> None:
        """Send TRANSFER_DONE opcode followed by the SHA-256 hex digest.

        Wire format:
            1B  Opcode.TRANSFER_DONE
            2B  digest length (uint16, always 64)
            64B ASCII hex digest
        """
        digest       = checksum.compute(file_data)
        digest_bytes = digest.encode("ascii")
        await self._write(
            Opcode.TRANSFER_DONE
            + struct.pack("!H", len(digest_bytes))
            + digest_bytes
        )
        logger.info(
            "Session %d — TRANSFER_DONE sent. checksum=%s…",
            self.client_id, digest[:16],
        )

    # ------------------------------------------------------------------
    # Response reader
    # ------------------------------------------------------------------

    async def _read_response(self) -> AckMessage | NackMessage:
        """Read one ACK or NACK from the client.

        A timeout is treated as a NACK with the sentinel seq_num 0xFFFFFFFF,
        meaning "I missed something — retransmit the current chunk."

        Raises:
            ProtocolError:           On unexpected opcode.
            ClientDisconnectedError: On connection drop.
        """
        try:
            opcode = await asyncio.wait_for(
                self._read_exact(1), timeout=config.RETRY_TIMEOUT
            )
        except asyncio.TimeoutError:
            return NackMessage(client_id=self.client_id, seq_num=0xFFFFFFFF)

        payload = await self._read_exact(AckMessage._SIZE)

        if opcode == Opcode.ACK:
            return AckMessage.from_bytes(payload)
        if opcode == Opcode.NACK:
            return NackMessage.from_bytes(payload)

        raise ProtocolError(f"Unexpected opcode in response: 0x{opcode.hex()}")

    # ------------------------------------------------------------------
    # Error reporting
    # ------------------------------------------------------------------

    async def _send_error(self, message: str) -> None:
        encoded = message.encode("utf-8")[:255]
        await self._write(Opcode.ERROR + struct.pack("!B", len(encoded)) + encoded)

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    async def _read_exact(self, n: int) -> bytes:
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ClientDisconnectedError(self.client_id) from exc

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def _close(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass