# =============================================================================
# main.py — Entry point for the file transfer system.
#
# What this does:
#   1. Configures logging for the whole process.
#   2. Creates sample test files if none are provided.
#   3. Starts the server as a background asyncio Task.
#   4. Runs N client transfers concurrently via asyncio.gather().
#   5. Prints a per-client summary when all clients finish.
#
# Usage:
#   python main.py                        # runs built-in demo
#   python main.py file1.txt file2.bin    # transfer specific files
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import config
from server.server import FileTransferServer
from client.client import FileTransferClient


# ---------------------------------------------------------------------------
# Logging setup — must happen before any module imports that call getLogger()
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT,
    )


# ---------------------------------------------------------------------------
# Demo file creation
# ---------------------------------------------------------------------------

def _create_demo_files() -> list[Path]:
    """Create small demo files in /tmp for the built-in demo run."""
    files = []
    demo_dir = Path("/tmp/ft_demo")
    demo_dir.mkdir(parents=True, exist_ok=True)

    specs = [
        ("data1.txt", b"Hello from Client A! " * 250),        # ~5 KB
        ("data2.bin", os.urandom(8192)),                       # 8 KB random
        ("data3.txt", b"Tiny file."),                          # 10 bytes
    ]
    for name, content in specs:
        path = demo_dir / name
        path.write_bytes(content)
        files.append(path)

    return files


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

async def _run_server() -> None:
    """Start the FileTransferServer.  Runs until cancelled."""
    server = FileTransferServer()
    await server.serve_forever()


# ---------------------------------------------------------------------------
# Client runner
# ---------------------------------------------------------------------------

async def _run_clients(file_paths: list[Path]) -> list[tuple[int, str, bool]]:
    """Run one client per file, all concurrently.

    Returns:
        List of (client_id, filename, success) tuples.
    """
    # Give the server a moment to bind its socket before clients connect.
    await asyncio.sleep(0.2)

    tasks = [
        FileTransferClient(client_id=idx + 1, file_path=path).run()
        for idx, path in enumerate(file_paths)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)

    return [
        (idx + 1, path.name, success)
        for idx, (path, success) in enumerate(zip(file_paths, results))
    ]


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(results: list[tuple[int, str, bool]], elapsed: float) -> None:
    passed = sum(1 for _, _, ok in results if ok)
    total  = len(results)

    print("\n" + "=" * 60)
    print(f"  Transfer Summary   ({elapsed:.2f}s)")
    print("=" * 60)
    for client_id, filename, ok in results:
        status = "✓  Successful" if ok else "✗  FAILED"
        print(f"  Client {client_id:>2}  {filename:<30}  {status}")
    print("-" * 60)
    print(f"  {passed} / {total} transfers succeeded.")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main(file_paths: list[Path]) -> None:
    logger = logging.getLogger(__name__)

    # Start the server as a background task.
    server_task = asyncio.create_task(_run_server(), name="file-transfer-server")
    logger.info("Server task created.")

    start = time.perf_counter()
    try:
        results = await _run_clients(file_paths)
    finally:
        # Cancel the server once all clients are done.
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            logger.info("Server stopped.")

    elapsed = time.perf_counter() - start
    _print_summary(results, elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _configure_logging()

    if len(sys.argv) > 1:
        paths = [Path(p) for p in sys.argv[1:]]
    else:
        print("No files provided — creating demo files…\n")
        paths = _create_demo_files()

    asyncio.run(main(paths))