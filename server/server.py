# =============================================================================
# server/server.py — Async TCP server.
#
# Responsibilities:
#   - Bind and listen on HOST:PORT.
#   - Accept incoming connections.
#   - Assign a unique client_id to each connection.
#   - Spawn an isolated ClientSession coroutine per client.
#   - Ensure one session's failure never affects others.
#
# The server itself is stateless beyond the incrementing client_id counter.
# All per-client state lives inside ClientSession.
# =============================================================================

from __future__ import annotations

import asyncio
import itertools
import logging
import signal
import sys

import config
from server.session import ClientSession

logger = logging.getLogger(__name__)


class FileTransferServer:
    """Async TCP server that dispatches each connection to a ClientSession.

    Usage:
        server = FileTransferServer()
        asyncio.run(server.serve_forever())
    """

    def __init__(self) -> None:
        # Thread-safe incrementing counter — itertools.count is atomic in CPython.
        self._client_id_counter = itertools.count(start=1)
        self._server: asyncio.Server | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def serve_forever(self) -> None:
        """Start the server and run until a shutdown signal is received."""
        self._server = await asyncio.start_server(
            client_connected_cb=self._on_client_connected,
            host=config.HOST,
            port=config.PORT,
            backlog=config.BACKLOG,
        )

        addr = self._server.sockets[0].getsockname()
        logger.info("Server listening on %s:%s", *addr)

        # Register graceful shutdown signals.
        # add_signal_handler() is Unix-only — Windows ProactorEventLoop does not
        # implement it and raises NotImplementedError.  We guard with a platform
        # check so the server runs correctly on both Windows and Unix.
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown)

        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _on_client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Called by asyncio for every new TCP connection.

        Each call runs as its own Task inside the event loop, so clients
        are fully concurrent without any manual task management here.

        Args:
            reader: Async byte stream from the client.
            writer: Async byte stream to the client.
        """
        client_id = next(self._client_id_counter)
        peer = writer.get_extra_info("peername")
        logger.info("Accepted client %d from %s", client_id, peer)

        session = ClientSession(
            client_id=client_id,
            reader=reader,
            writer=writer,
        )

        # Run the session; catch everything so the server loop stays alive.
        try:
            await session.run()
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unhandled exception in session %d: %s", client_id, exc
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        """Initiate graceful shutdown."""
        logger.info("Shutdown signal received — stopping server.")
        if self._server:
            self._server.close()