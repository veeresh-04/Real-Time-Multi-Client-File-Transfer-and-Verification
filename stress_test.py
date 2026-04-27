# stress_test.py — spin up N clients simultaneously against a live server

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import config
config.SIMULATE_ERRORS   = True
config.DROP_PROBABILITY  = 0.10
config.CORRUPT_PROBABILITY = 0.05
config.MAX_RETRIES       = 10

logging.basicConfig(
    level=logging.WARNING,   # set to INFO to see all chunk logs
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from server.server import FileTransferServer
from client.client import FileTransferClient

NUM_CLIENTS = 100          # ← change this to test more
FILE_SIZE_KB = 1024      # ← change this to test larger files

async def main():
    # Create temp files
    tmp = Path("stress_tmp")
    tmp.mkdir(exist_ok=True)
    files = []
    for i in range(NUM_CLIENTS):
        f = tmp / f"stress_{i}.bin"
        f.write_bytes(os.urandom(FILE_SIZE_KB * 1024))
        files.append(f)

    # Start server
    server_task = asyncio.create_task(FileTransferServer().serve_forever())
    await asyncio.sleep(0.2)

    print(f"\nRunning {NUM_CLIENTS} concurrent clients × {FILE_SIZE_KB}KB each")
    print(f"Drop={config.DROP_PROBABILITY*100:.0f}%  Corrupt={config.CORRUPT_PROBABILITY*100:.0f}%\n")

    start = time.perf_counter()
    results = await asyncio.gather(*[
        FileTransferClient(client_id=i+1, file_path=f).run()
        for i, f in enumerate(files)
    ])
    elapsed = time.perf_counter() - start

    server_task.cancel()

    passed = sum(results)
    total_mb = NUM_CLIENTS * FILE_SIZE_KB / 1024
    print(f"\n{'='*50}")
    print(f"  {passed}/{NUM_CLIENTS} clients succeeded")
    print(f"  Total data: {total_mb:.1f} MB")
    print(f"  Time: {elapsed:.2f}s")
    print(f"  Throughput: {total_mb/elapsed:.2f} MB/s")
    print(f"{'='*50}\n")

asyncio.run(main())