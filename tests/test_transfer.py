# =============================================================================
# tests/test_transfer.py — Integration tests: real server ↔ client transfers.
#
# Each test spins up a real async TCP server on a random port, runs one or
# more clients against it, then tears everything down.  No mocking.
# =============================================================================

from __future__ import annotations

import asyncio
import os
import pytest
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config
# Override port to avoid collisions with a running demo
config.PORT = 19001
config.SIMULATE_ERRORS = False      # clean runs for functional tests
config.MAX_RETRIES = 3

from server.server import FileTransferServer
from client.client import FileTransferClient


# ---------------------------------------------------------------------------
# Shared fixture: server running on the test port
# ---------------------------------------------------------------------------

@pytest.fixture()
async def running_server():
    """Start the server, yield control, then cancel."""
    server = FileTransferServer()
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.15)       # let the socket bind
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(tmp_path, name: str, data: bytes) -> pathlib.Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


async def _transfer(client_id: int, path: pathlib.Path) -> bool:
    return await FileTransferClient(client_id=client_id, file_path=path).run()


# ---------------------------------------------------------------------------
# Functional tests — single client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSingleClientTransfer:

    async def test_small_text_file(self, running_server, tmp_path):
        f = _write_tmp(tmp_path, "small.txt", b"Hello, world!")
        assert await _transfer(1, f) is True

    async def test_exact_chunk_boundary(self, running_server, tmp_path):
        # Exactly 3 × 1024 bytes — all chunks full, no partial last chunk.
        f = _write_tmp(tmp_path, "boundary.bin", b"A" * 3072)
        assert await _transfer(2, f) is True

    async def test_one_byte_file(self, running_server, tmp_path):
        f = _write_tmp(tmp_path, "one.bin", b"\xff")
        assert await _transfer(3, f) is True

    async def test_empty_file(self, running_server, tmp_path):
        f = _write_tmp(tmp_path, "empty.bin", b"")
        assert await _transfer(4, f) is True

    async def test_random_binary_data(self, running_server, tmp_path):
        f = _write_tmp(tmp_path, "random.bin", os.urandom(5000))
        assert await _transfer(5, f) is True

    async def test_1mb_file(self, running_server, tmp_path):
        f = _write_tmp(tmp_path, "large.bin", os.urandom(1024 * 1024))
        assert await _transfer(6, f) is True

    async def test_missing_file_returns_false(self, running_server, tmp_path):
        result = await _transfer(7, tmp_path / "nonexistent.txt")
        assert result is False


# ---------------------------------------------------------------------------
# Error simulation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestErrorSimulation:

    async def test_transfer_succeeds_with_drop_simulation(self, tmp_path):
        """Transfers should succeed even with packet drops and retries."""
        config.SIMULATE_ERRORS  = True
        config.DROP_PROBABILITY = 0.2      # 20% drop rate
        config.CORRUPT_PROBABILITY = 0.0
        config.PORT = 19002

        server = FileTransferServer()
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.15)

        try:
            f = _write_tmp(tmp_path, "drop_test.bin", os.urandom(4096))
            result = await _transfer(1, f)
            assert result is True
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            config.SIMULATE_ERRORS  = False
            config.PORT = 19001