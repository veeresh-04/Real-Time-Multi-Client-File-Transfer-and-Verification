# =============================================================================
# tests/test_concurrency.py — Concurrency tests for the file transfer system.
#
# These tests verify that:
#   1. Multiple clients can transfer simultaneously without interfering.
#   2. Each client's data is isolated — no cross-contamination.
#   3. Clients with different file sizes finish independently.
#   4. One client's failure does not affect others.
#   5. The server stays alive across many sequential and concurrent sessions.
#   6. High client counts work within the server's BACKLOG limit.
#   7. Transfers under error simulation succeed concurrently.
#
# Every test runs a real async server on a dedicated port so tests are
# fully isolated from each other and from test_transfer.py.
# =============================================================================

from __future__ import annotations

import asyncio
import os
import pathlib
import time
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import config

config.SIMULATE_ERRORS = False
config.MAX_RETRIES     = 5

from server.server import FileTransferServer
from client.client import FileTransferClient


# ---------------------------------------------------------------------------
# Port registry — each fixture class uses its own port to avoid conflicts
# ---------------------------------------------------------------------------

_BASE_PORT = 19100   # concurrency tests start here, away from test_transfer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_file(tmp_path: pathlib.Path, name: str, data: bytes) -> pathlib.Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


async def _start_server(port: int) -> tuple[FileTransferServer, asyncio.Task]:
    """Start a server on *port*, wait for the socket to bind, return task."""
    config.PORT = port
    server = FileTransferServer()
    task = asyncio.create_task(server.serve_forever(), name=f"server-{port}")
    await asyncio.sleep(0.15)
    return server, task


async def _stop_server(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _run(client_id: int, path: pathlib.Path) -> bool:
    return await FileTransferClient(client_id=client_id, file_path=path).run()


async def _run_all(*args: tuple[int, pathlib.Path]) -> list[bool]:
    """Run multiple (client_id, path) pairs concurrently."""
    return list(
        await asyncio.gather(*[_run(cid, p) for cid, p in args])
    )


# ---------------------------------------------------------------------------
# 1. Basic simultaneous transfers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSimultaneousTransfers:
    """Core concurrent behaviour — multiple clients at the same time."""

    PORT = _BASE_PORT

    async def test_two_clients_same_size(self, tmp_path):
        _, task = await _start_server(self.PORT)
        try:
            f1 = _tmp_file(tmp_path, "a.bin", b"Client A " * 600)
            f2 = _tmp_file(tmp_path, "b.bin", b"Client B " * 600)
            r1, r2 = await _run_all((1, f1), (2, f2))
            assert r1 is True, "Client 1 failed"
            assert r2 is True, "Client 2 failed"
        finally:
            await _stop_server(task)

    async def test_two_clients_different_sizes(self, tmp_path):
        _, task = await _start_server(self.PORT)
        try:
            f1 = _tmp_file(tmp_path, "small.bin", b"S" * 512)          # < 1 chunk
            f2 = _tmp_file(tmp_path, "large.bin", os.urandom(50_000))   # ~49 chunks
            r1, r2 = await _run_all((1, f1), (2, f2))
            assert r1 is True
            assert r2 is True
        finally:
            await _stop_server(task)

    async def test_five_clients_mixed_files(self, tmp_path):
        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"mix_{i}.bin", os.urandom(1024 * (2 ** i)))
                for i in range(5)     # 1KB, 2KB, 4KB, 8KB, 16KB
            ]
            results = await _run_all(*enumerate(files, start=1))
            assert all(results), f"Failures: {[i+1 for i,r in enumerate(results) if not r]}"
        finally:
            await _stop_server(task)

    async def test_ten_clients_simultaneously(self, tmp_path):
        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"t{i}.bin", os.urandom(3000 + i * 300))
                for i in range(10)
            ]
            results = await _run_all(*enumerate(files, start=1))
            failed = [i + 1 for i, r in enumerate(results) if not r]
            assert not failed, f"Clients {failed} failed"
        finally:
            await _stop_server(task)

    async def test_twenty_clients_simultaneously(self, tmp_path):
        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"c{i}.bin", os.urandom(2048))
                for i in range(20)
            ]
            results = await _run_all(*enumerate(files, start=1))
            failed = [i + 1 for i, r in enumerate(results) if not r]
            assert not failed, f"Clients {failed} failed"
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# 2. Data isolation — no cross-contamination between sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDataIsolation:
    """Verify each client receives exactly its own file content."""

    PORT = _BASE_PORT + 1

    async def test_clients_receive_their_own_data(self, tmp_path):
        """Each client's output file must match its own input exactly."""
        _, task = await _start_server(self.PORT)
        try:
            payloads = {
                i + 1: os.urandom(4096) for i in range(5)
            }
            files = [
                _tmp_file(tmp_path, f"iso_{cid}.bin", data)
                for cid, data in payloads.items()
            ]

            results = await _run_all(*enumerate(files, start=1))
            assert all(results), "Some transfers failed during isolation test"

            # Verify each output file contains the correct data
            out_dir = pathlib.Path(config.CLIENT_OUTPUT_DIR)
            for cid, original in payloads.items():
                out_file = out_dir / f"client_{cid}_iso_{cid}.bin"
                assert out_file.exists(), f"Output missing for client {cid}"
                assert out_file.read_bytes() == original, (
                    f"Client {cid} received wrong data — session bleed detected!"
                )
        finally:
            await _stop_server(task)

    async def test_identical_filenames_different_clients(self, tmp_path):
        """Two clients uploading files with the same name get isolated output."""
        _, task = await _start_server(self.PORT)
        try:
            data1 = b"DATA_FOR_CLIENT_ONE " * 200
            data2 = b"DATA_FOR_CLIENT_TWO " * 200
            # Both files named "upload.bin" — server must keep them separate
            (tmp_path / "c1").mkdir(exist_ok=True)
            (tmp_path / "c2").mkdir(exist_ok=True)
            f1 = _tmp_file(tmp_path / "c1", "upload.bin", data1)
            f2 = _tmp_file(tmp_path / "c2", "upload.bin", data2)

            r1, r2 = await _run_all((1, f1), (2, f2))
            assert r1 is True
            assert r2 is True

            out_dir = pathlib.Path(config.CLIENT_OUTPUT_DIR)
            out1 = out_dir / "client_1_upload.bin"
            out2 = out_dir / "client_2_upload.bin"
            assert out1.read_bytes() == data1
            assert out2.read_bytes() == data2
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# 3. Sequential sessions — server stays alive between runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSequentialSessions:
    """Server must remain stable across many back-to-back client connections."""

    PORT = _BASE_PORT + 2

    async def test_ten_sequential_transfers(self, tmp_path):
        """Ten clients one after another — server must not degrade."""
        _, task = await _start_server(self.PORT)
        try:
            for i in range(10):
                f = _tmp_file(tmp_path, f"seq_{i}.bin", os.urandom(2048))
                result = await _run(i + 1, f)
                assert result is True, f"Sequential client {i+1} failed"
        finally:
            await _stop_server(task)

    async def test_server_accepts_after_client_error(self, tmp_path):
        """A failed client (missing file) must not stop the server serving the next."""
        _, task = await _start_server(self.PORT)
        try:
            # Client 1 — bad file path (will fail)
            bad_result = await _run(1, tmp_path / "ghost.bin")
            assert bad_result is False

            # Client 2 — good file (must still succeed)
            good_file = _tmp_file(tmp_path, "good.bin", b"still alive" * 100)
            good_result = await _run(2, good_file)
            assert good_result is True, "Server failed after a bad client"
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# 4. Mixed good/bad clients — failures are isolated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFailureIsolation:
    """A failing client must not affect concurrent successful clients."""

    PORT = _BASE_PORT + 3

    async def test_bad_client_does_not_poison_good_clients(self, tmp_path):
        """One client with a missing file runs alongside three valid ones."""
        _, task = await _start_server(self.PORT)
        try:
            good1 = _tmp_file(tmp_path, "g1.bin", os.urandom(3000))
            good2 = _tmp_file(tmp_path, "g2.bin", os.urandom(5000))
            good3 = _tmp_file(tmp_path, "g3.bin", os.urandom(2000))
            ghost = tmp_path / "missing.bin"   # does not exist

            bad, r1, r2, r3 = await asyncio.gather(
                _run(99, ghost),
                _run(1, good1),
                _run(2, good2),
                _run(3, good3),
            )

            assert bad is False, "Expected bad client to fail"
            assert r1  is True,  "Good client 1 was affected by bad client"
            assert r2  is True,  "Good client 2 was affected by bad client"
            assert r3  is True,  "Good client 3 was affected by bad client"
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# 5. Concurrent transfers under error simulation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestConcurrentWithErrors:
    """Multiple clients must all succeed even with drop/corrupt simulation."""

    PORT = _BASE_PORT + 4

    async def test_five_clients_with_drops(self, tmp_path):
        orig_simulate = config.SIMULATE_ERRORS
        orig_drop     = config.DROP_PROBABILITY
        orig_corrupt  = config.CORRUPT_PROBABILITY
        orig_retries  = config.MAX_RETRIES

        config.SIMULATE_ERRORS     = True
        config.DROP_PROBABILITY    = 0.15
        config.CORRUPT_PROBABILITY = 0.0
        config.MAX_RETRIES         = 10

        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"drop_{i}.bin", os.urandom(3072))
                for i in range(5)
            ]
            results = await _run_all(*enumerate(files, start=1))
            failed = [i + 1 for i, r in enumerate(results) if not r]
            assert not failed, f"Clients {failed} failed under drop simulation"
        finally:
            await _stop_server(task)
            config.SIMULATE_ERRORS     = orig_simulate
            config.DROP_PROBABILITY    = orig_drop
            config.CORRUPT_PROBABILITY = orig_corrupt
            config.MAX_RETRIES         = orig_retries

    async def test_five_clients_with_corruption(self, tmp_path):
        # Snapshot original values so finally block always restores them,
        # even if the test body raises before completing every assignment.
        orig_simulate  = config.SIMULATE_ERRORS
        orig_drop      = config.DROP_PROBABILITY
        orig_corrupt   = config.CORRUPT_PROBABILITY
        orig_retries   = config.MAX_RETRIES
        orig_port      = config.PORT

        config.SIMULATE_ERRORS     = True
        config.DROP_PROBABILITY    = 0.0
        config.CORRUPT_PROBABILITY = 0.10
        config.MAX_RETRIES         = 10
        # Use a dedicated port — avoids interference from the drops test
        # which may still be tearing down its server on self.PORT.
        config.PORT                = self.PORT + 100

        _, task = await _start_server(config.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"corrupt_{i}.bin", os.urandom(3072))
                for i in range(5)
            ]
            results = await _run_all(*enumerate(files, start=1))
            failed = [i + 1 for i, r in enumerate(results) if not r]
            assert not failed, f"Clients {failed} failed under corruption simulation"
        finally:
            await _stop_server(task)
            config.SIMULATE_ERRORS     = orig_simulate
            config.DROP_PROBABILITY    = orig_drop
            config.CORRUPT_PROBABILITY = orig_corrupt
            config.MAX_RETRIES         = orig_retries
            config.PORT                = orig_port


# ---------------------------------------------------------------------------
# 6. Performance / throughput baseline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPerformance:
    """Sanity-check that throughput is within reasonable bounds."""

    PORT = _BASE_PORT + 5

    async def test_1mb_file_completes_under_5_seconds(self, tmp_path):
        config.SIMULATE_ERRORS = False
        _, task = await _start_server(self.PORT)
        try:
            f = _tmp_file(tmp_path, "perf_1mb.bin", os.urandom(1024 * 1024))
            start = time.perf_counter()
            result = await _run(1, f)
            elapsed = time.perf_counter() - start
            assert result is True
            assert elapsed < 5.0, f"1 MB transfer took {elapsed:.2f}s — too slow"
        finally:
            await _stop_server(task)

    async def test_five_clients_1mb_each_under_10_seconds(self, tmp_path):
        config.SIMULATE_ERRORS = False
        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"perf_{i}.bin", os.urandom(1024 * 1024))
                for i in range(5)
            ]
            start = time.perf_counter()
            results = await _run_all(*enumerate(files, start=1))
            elapsed = time.perf_counter() - start
            assert all(results), "Some 1MB transfers failed"
            assert elapsed < 10.0, (
                f"5× 1MB concurrent transfers took {elapsed:.2f}s — too slow"
            )
        finally:
            await _stop_server(task)


# ---------------------------------------------------------------------------
# 7. Out-of-order packet delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestOutOfOrderDelivery:
    """Chunks arrive in scrambled order — client must reassemble correctly."""

    PORT = _BASE_PORT + 6

    async def test_single_client_with_reordering(self, tmp_path):
        orig_simulate = config.SIMULATE_ERRORS
        orig_reorder  = getattr(config, "REORDER_PROBABILITY", 0.05)
        orig_drop     = config.DROP_PROBABILITY
        orig_corrupt  = config.CORRUPT_PROBABILITY

        config.SIMULATE_ERRORS     = True
        config.REORDER_PROBABILITY = 0.3    # aggressively reorder 30% of adjacent pairs
        config.DROP_PROBABILITY    = 0.0
        config.CORRUPT_PROBABILITY = 0.0

        _, task = await _start_server(self.PORT)
        try:
            # Use a multi-chunk file so reordering is meaningful
            f = _tmp_file(tmp_path, "reorder.bin", os.urandom(1024 * 1024))
            result = await _run(1, f)
            assert result is True, "Transfer failed with out-of-order chunks"
        finally:
            await _stop_server(task)
            config.SIMULATE_ERRORS     = orig_simulate
            config.REORDER_PROBABILITY = orig_reorder
            config.DROP_PROBABILITY    = orig_drop
            config.CORRUPT_PROBABILITY = orig_corrupt

    async def test_five_clients_with_reordering(self, tmp_path):
        orig_simulate = config.SIMULATE_ERRORS
        orig_reorder  = getattr(config, "REORDER_PROBABILITY", 0.05)
        orig_drop     = config.DROP_PROBABILITY
        orig_corrupt  = config.CORRUPT_PROBABILITY

        config.SIMULATE_ERRORS     = True
        config.REORDER_PROBABILITY = 0.2
        config.DROP_PROBABILITY    = 0.0
        config.CORRUPT_PROBABILITY = 0.0

        _, task = await _start_server(self.PORT)
        try:
            files = [
                _tmp_file(tmp_path, f"reorder_{i}.bin", os.urandom(512 * 1024))
                for i in range(5)
            ]
            results = await _run_all(*enumerate(files, start=1))
            failed = [i + 1 for i, r in enumerate(results) if not r]
            assert not failed, f"Clients {failed} failed with out-of-order chunks"
        finally:
            await _stop_server(task)
            config.SIMULATE_ERRORS     = orig_simulate
            config.REORDER_PROBABILITY = orig_reorder
            config.DROP_PROBABILITY    = orig_drop
            config.CORRUPT_PROBABILITY = orig_corrupt