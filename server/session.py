# =============================================================================
# server/session.py — Async per-client file transfer session.
#
# One ClientSession instance is created per connected client.
# It owns the full lifecycle for that client:
#   1. Receive the uploaded file from the client.
#   2. Persist it to disk.
#   3. Split it into chunks.
#   4. Send chunks back (with simulated faults).
#   5. Handle NACK-driven retransmission.
#   6. Send the final checksum.
#
# Nothing in here knows about other clients — full isolation by design.
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import struct
from pathlib import Path

import config
from core import checksum
from core.errors import ClientDisconnectedError, MaxRetriesExceededError, ProtocolError
from core.protocol import (
    AckMessage,
    Chunk,
    NackMessage,
    Opcode,
    split_file_into_chunks,
)
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
        self._reader = reader
        self._writer = writer
        self._peer = writer.get_extra_info("peername")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drive the session from upload through delivery.

        Called by the server for each accepted connection.
        Any unhandled exception here is caught by the server so it never
        brings down the event loop.
        """
        logger.info("Session %d started — peer %s", self.client_id, self._peer)
        try:
            file_data = await self._receive_upload()
            chunks = await self._prepare_chunks(file_data)
            await self._deliver_chunks(chunks)
            await self._send_transfer_done(file_data)
            logger.info("Session %d completed successfully.", self.client_id)
        except (ClientDisconnectedError, MaxRetriesExceededError, ProtocolError) as exc:
            logger.error("Session %d failed: %s", self.client_id, exc)
            await self._send_error(str(exc))
        finally:
            await self._close()

    # ------------------------------------------------------------------
    # Phase 1 — Receive the file upload from the client
    # ------------------------------------------------------------------

    async def _receive_upload(self) -> bytes:
        """Read the UPLOAD_REQUEST message and return raw file bytes.

        Wire format of UPLOAD_REQUEST:
            1 byte  — Opcode.UPLOAD_REQUEST (0x01)
            4 bytes — file size (uint32, network order)
            N bytes — raw file data

        Returns:
            Complete file contents as bytes.

        Raises:
            ProtocolError:          If the opcode is wrong.
            ClientDisconnectedError: If the client drops mid-upload.
        """
        # --- read and validate the opcode ---
        opcode = await self._read_exact(1)
        if opcode != Opcode.UPLOAD_REQUEST:
            raise ProtocolError(
                f"Expected UPLOAD_REQUEST (0x01), got 0x{opcode.hex()} "
                f"from client {self.client_id}."
            )

        # --- read 4-byte file size ---
        raw_size = await self._read_exact(4)
        (file_size,) = struct.unpack("!I", raw_size)
        logger.info(
            "Session %d — receiving upload: %d bytes", self.client_id, file_size
        )

        # --- stream the file payload ---
        file_data = await self._read_exact(file_size)
        logger.debug("Session %d — upload complete.", self.client_id)

        # --- save to disk ---
        await self._persist_file(file_data)
        return file_data

    async def _persist_file(self, data: bytes) -> None:
        """Write the received file to SERVER_STORAGE_DIR."""
        storage = Path(config.SERVER_STORAGE_DIR)
        storage.mkdir(parents=True, exist_ok=True)
        dest = storage / f"client_{self.client_id}.bin"
        # Run blocking file I/O in the thread pool so we don't stall the loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dest.write_bytes, data)
        logger.debug("Session %d — file saved to %s", self.client_id, dest)

    # ------------------------------------------------------------------
    # Phase 2 — Prepare chunks
    # ------------------------------------------------------------------

    async def _prepare_chunks(self, file_data: bytes) -> list[Chunk]:
        """Split the file and return the ordered chunk list."""
        chunks = split_file_into_chunks(file_data, self.client_id)
        logger.info(
            "Session %d — split into %d chunk(s).", self.client_id, len(chunks)
        )
        return chunks

    # ------------------------------------------------------------------
    # Phase 3 — Deliver chunks with retransmission on NACK
    # ------------------------------------------------------------------

    async def _deliver_chunks(self, chunks: list[Chunk]) -> None:
        """Send every chunk and honour NACK-driven retransmission requests.

        For each chunk:
          - Optionally drop or corrupt it (simulation).
          - Send it.
          - Wait for ACK or NACK.
          - On NACK or timeout: retransmit the original (clean) chunk.
          - Raise MaxRetriesExceededError after MAX_RETRIES failures.
        """
        for chunk in chunks:
            await self._send_chunk_with_retry(chunk)

    async def _send_chunk_with_retry(self, chunk: Chunk) -> None:
        """Send one chunk, retrying up to MAX_RETRIES times on failure.

        Args:
            chunk: The clean (un-simulated) chunk to eventually deliver.

        Raises:
            MaxRetriesExceededError: When all retry attempts are exhausted.
        """
        for attempt in range(1, config.MAX_RETRIES + 1):
            # Step 1: drop decision — uses the clean chunk.
            dropped = maybe_drop_wrapper(chunk)

            if dropped is None:
                logger.debug(
                    "Session %d — chunk %d dropped (attempt %d).",
                    self.client_id, chunk.seq_num, attempt,
                )
            else:
                # Step 2: corruption — mutates payload bytes only.
                # IMPORTANT: to_bytes() must be called on the CLEAN chunk so
                # the CRC in the header reflects the intended payload.
                # We then replace the payload bytes after the header with the
                # corrupted payload so the client's CRC check detects the fault.
                corrupted = maybe_corrupt(dropped)
                clean_wire  = chunk.to_bytes()          # header has correct CRC
                if corrupted.payload != chunk.payload:
                    # Splice corrupted payload after the clean header
                    wire = clean_wire[: chunk._HEADER_SIZE] + corrupted.payload
                else:
                    wire = clean_wire

                payload_len = struct.pack("!H", len(corrupted.payload))
                await self._write(Opcode.CHUNK_DATA + payload_len + wire)

            # Wait for client response
            response = await self._read_response()

            if isinstance(response, AckMessage):
                logger.debug(
                    "Session %d — chunk %d ACK'd.", self.client_id, chunk.seq_num
                )
                return  # ← success, move to next chunk

            if isinstance(response, NackMessage):
                logger.warning(
                    "Session %d — chunk %d NACK'd (attempt %d/%d), retransmitting.",
                    self.client_id, chunk.seq_num, attempt, config.MAX_RETRIES,
                )
                continue  # ← retry with clean chunk

        raise MaxRetriesExceededError(chunk.seq_num, config.MAX_RETRIES)

    async def _read_response(self) -> AckMessage | NackMessage:
        """Read one ACK or NACK from the client.

        Returns:
            An AckMessage or NackMessage.

        Raises:
            ProtocolError:          On unexpected opcode.
            ClientDisconnectedError: On connection drop.
        """
        try:
            opcode = await asyncio.wait_for(
                self._read_exact(1), timeout=config.RETRY_TIMEOUT
            )
        except asyncio.TimeoutError:
            # Treat timeout as a NACK — client missed the chunk.
            return NackMessage(client_id=self.client_id, seq_num=-1)

        payload = await self._read_exact(AckMessage._SIZE)

        if opcode == Opcode.ACK:
            return AckMessage.from_bytes(payload)
        if opcode == Opcode.NACK:
            return NackMessage.from_bytes(payload)

        raise ProtocolError(f"Unexpected opcode in response: 0x{opcode.hex()}")

    # ------------------------------------------------------------------
    # Phase 4 — Signal completion + send checksum
    # ------------------------------------------------------------------

    async def _send_transfer_done(self, file_data: bytes) -> None:
        """Send TRANSFER_DONE opcode followed by the SHA-256 hex digest.

        Wire format:
            1 byte   — Opcode.TRANSFER_DONE (0x05)
            2 bytes  — digest length (uint16, always 64 for SHA-256)
            64 bytes — ASCII hex digest
        """
        digest = checksum.compute(file_data)
        digest_bytes = digest.encode("ascii")  # always 64 bytes
        header = struct.pack("!H", len(digest_bytes))
        await self._write(Opcode.TRANSFER_DONE + header + digest_bytes)
        logger.info(
            "Session %d — TRANSFER_DONE sent. checksum=%s…", self.client_id, digest[:16]
        )

    # ------------------------------------------------------------------
    # Error reporting
    # ------------------------------------------------------------------

    async def _send_error(self, message: str) -> None:
        """Send an ERROR opcode with a UTF-8 message to the client."""
        encoded = message.encode("utf-8")[:255]   # cap at 255 bytes
        header = struct.pack("!B", len(encoded))
        await self._write(Opcode.ERROR + header + encoded)

    # ------------------------------------------------------------------
    # Low-level I/O helpers
    # ------------------------------------------------------------------

    async def _read_exact(self, n: int) -> bytes:
        """Read exactly *n* bytes from the stream.

        Raises:
            ClientDisconnectedError: If EOF arrives before *n* bytes.
        """
        try:
            data = await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ClientDisconnectedError(self.client_id) from exc
        return data

    async def _write(self, data: bytes) -> None:
        """Write *data* to the stream and flush."""
        self._writer.write(data)
        await self._writer.drain()

    async def _close(self) -> None:
        """Gracefully close the writer half of the connection."""
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass  # Already closed — nothing to do.


# ---------------------------------------------------------------------------
# Module-level helper (keeps session code clean)
# ---------------------------------------------------------------------------

def maybe_drop_wrapper(chunk: Chunk) -> Chunk | None:
    """Return None if the chunk should be dropped, else return the chunk."""
    from simulation.network import maybe_drop
    return None if maybe_drop(chunk) else chunk