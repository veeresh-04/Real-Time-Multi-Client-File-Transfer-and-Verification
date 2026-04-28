# Real-Time Multi-Client File Transfer and Verification

A multi-client file transfer system built in Python using **AsyncIO** and **TCP sockets**. Multiple clients upload files to the server concurrently. The server splits files into chunks, sends them back, and verifies integrity using SHA-256 checksums and per-chunk CRC32. Network faults — dropped packets, corrupted payloads, and out-of-order delivery — are simulated with full retransmission logic to recover from them.

---

## Features

- **Async TCP server** — single event loop handles 100+ concurrent clients with no thread overhead
- **Chunked transfer** — files split into 64 KB chunks, each tagged with sequence number, client ID, and CRC32
- **End-to-end integrity** — SHA-256 checksum verified after full reassembly; CRC32 verified per chunk
- **Seq_num-driven retransmission** — every ACK/NACK carries the explicit chunk sequence number; server never advances until the correct chunk is confirmed
- **Three fault simulations** — configurable packet drop, payload corruption, and out-of-order delivery
- **Full session isolation** — each client's state is completely independent; one failure never affects others
- **Cross-platform** — runs on Windows, Linux, and macOS

---

## Project Structure

```
file_transfer/
│
├── config.py                  # All tuneable parameters — one place, nothing hardcoded
│
├── core/
│   ├── protocol.py            # Wire format, chunk struct, CRC32, ACK/NACK, helpers
│   ├── checksum.py            # SHA-256 file integrity
│   └── errors.py              # Typed exception hierarchy
│
├── server/
│   ├── server.py              # Async TCP listener, connection dispatch
│   └── session.py             # Per-client session — upload, chunk, deliver, retransmit
│
├── client/
│   └── client.py              # Upload, receive chunks, ACK/NACK, reassemble, verify
│
├── simulation/
│   └── network.py             # Drop, corruption, and out-of-order injection
│
├── tests/
│   ├── test_protocol.py       # 22 unit tests — wire format, CRC32, checksums
│   ├── test_transfer.py       #  8 integration tests — single client, error simulation
│   └── test_concurrency.py   # 16 concurrency tests — isolation, stress, out-of-order
│
├── main.py                    # Entry point — demo or pass your own files
└── stress_test.py             # N-client stress test with throughput reporting
```

---

## Wire Protocol

Every byte on the wire is defined in `core/protocol.py`. The protocol is binary, big-endian (network byte order).

### Opcodes (1 byte)

| Opcode | Hex | Direction | Meaning |
|--------|-----|-----------|---------|
| UPLOAD_REQUEST | `0x01` | client → server | Start upload with file size |
| CHUNK_DATA     | `0x02` | server → client | Deliver one file chunk |
| ACK            | `0x03` | client → server | Chunk received OK |
| NACK           | `0x04` | client → server | Chunk failed, resend |
| TRANSFER_DONE  | `0x05` | server → client | All chunks sent + checksum |
| ERROR          | `0x06` | both directions | Session-level error |

### UPLOAD_REQUEST layout

```
1B   0x01         opcode
4B   file_size    uint32, network order
NB   file_data    raw bytes
```

### CHUNK_DATA layout

```
1B   0x02          opcode
2B   payload_len   uint16, network order
──── chunk header (17 bytes) ────────────────
4B   client_id     uint32
4B   seq_num       uint32  (zero-based)
4B   total_chunks  uint32
1B   is_last       bool
4B   crc32         uint32  CRC of the CLEAN payload (corruption detection)
─────────────────────────────────────────────
NB   payload       raw bytes (≤ 65536)
```

### ACK / NACK layout

```
1B   0x03 or 0x04   opcode
4B   client_id      uint32
4B   seq_num        uint32  (the exact chunk being acknowledged)
```

> **Timeout NACK:** When a client times out waiting for a chunk it sends
> `NACK(seq_num=0xFFFFFFFF)` — a sentinel meaning "resend the current chunk."

### TRANSFER_DONE layout

```
1B   0x05         opcode
2B   digest_len   uint16 (always 64 for SHA-256)
64B  digest       ASCII hex string
```

---

## Retransmission Design

The protocol is **seq_num-driven**. The server never advances to chunk `N+1` until it receives `ACK(seq_num=N)`.

```
Server                              Client
──────                              ──────
send chunk N  ──────────────────►
                                    CRC32 check passes
              ◄──────────────────  ACK(seq_num=N)
send chunk N+1 ─────────────────►  ...

── on drop ──────────────────────────────────────────
send chunk N  ──────────────── ✗   (silently dropped)
              ◄──────────────────  NACK(seq_num=0xFFFFFFFF)  [timeout]
send chunk N  ──────────────────►  (retransmit clean original)
                                   ACK(seq_num=N)  ✓

── on corruption ────────────────────────────────────
send chunk N  ──────────────────►  (payload byte flipped)
                                   CRC32 mismatch detected
              ◄──────────────────  NACK(seq_num=N)
send chunk N  ──────────────────►  (retransmit clean original)
                                   CRC32 passes → ACK(seq_num=N)  ✓

── on out-of-order ──────────────────────────────────
server sends chunks in scrambled order (N+1 before N)
client stores each by seq_num in a dict
client reassembles by sorted seq_num after all chunks received  ✓
```

The CRC in the chunk header is always computed from the **clean payload** before any simulation faults are applied. If corruption is simulated, the header CRC reflects what the payload *should* be — the client detects the mismatch immediately and NACKs.

---

## Installation

```bash
git clone https://github.com/veeresh-04/Real-Time-Multi-Client-File-Transfer-and-Verification.git
cd Real-Time-Multi-Client-File-Transfer-and-Verification

# Create and activate a virtual environment (recommended)
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

# Install test dependencies
pip install pytest pytest-asyncio
```

No other dependencies — the entire system uses Python's standard library.

**Requires Python 3.10+**

---

## Usage

### Built-in demo — 3 concurrent clients

```bash
python main.py
```

Creates three sample files and transfers them concurrently:

```
============================================================
  Transfer Summary   (0.96s)
============================================================
  Client  1  data1.txt                       ✓  Successful
  Client  2  data2.bin                       ✓  Successful
  Client  3  data3.txt                       ✓  Successful
------------------------------------------------------------
  3 / 3 transfers succeeded.
============================================================
```

### Transfer your own files

```bash
python main.py file1.txt file2.pdf file3.bin
```

Each file gets its own client. All run concurrently.

### Separate terminals (watch server and clients live)

**Terminal 1 — server:**
```bash
python -c "
import asyncio, logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s', datefmt='%H:%M:%S')
from server.server import FileTransferServer
asyncio.run(FileTransferServer().serve_forever())
"
```

**Terminal 2 — Client A:**
```bash
python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s', datefmt='%H:%M:%S')
from client.client import FileTransferClient
result = asyncio.run(FileTransferClient(client_id=1, file_path='main.py').run())
print('Result:', 'SUCCESS' if result else 'FAILED')
"
```

**Terminal 3 — Client B (same time as Terminal 2):**
```bash
python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s', datefmt='%H:%M:%S')
from client.client import FileTransferClient
result = asyncio.run(FileTransferClient(client_id=2, file_path='config.py').run())
print('Result:', 'SUCCESS' if result else 'FAILED')
"
```

### Stress test — N concurrent clients

```bash
python stress_test.py
```

Edit the top of `stress_test.py` to control load:

```python
NUM_CLIENTS  = 100    # number of concurrent clients
FILE_SIZE_KB = 1024   # KB per client
```

Real result — 100 clients × 1 MB:
```
==================================================
  100/100 clients succeeded
  Total data: 100.0 MB
  Time: 3.43s
  Throughput: 29.13 MB/s
==================================================
```

---

## Configuration

All parameters live in `config.py`. Nothing is hardcoded anywhere else.

```python
# Network
HOST    = "127.0.0.1"
PORT    = 9000
BACKLOG = 50              # max queued OS connections

# Protocol
CHUNK_SIZE = 65536        # 64 KB per chunk — optimal for large files

# Reliability
MAX_RETRIES   = 10        # retries before giving up on a chunk
RETRY_TIMEOUT = 0.5       # seconds to wait for ACK before sending NACK

# Error Simulation  (set SIMULATE_ERRORS = False for clean transfers)
SIMULATE_ERRORS     = False  # master switch
DROP_PROBABILITY    = 0.05   # 5%  — chunk silently dropped
CORRUPT_PROBABILITY = 0.03   # 3%  — one payload byte XOR'd with 0xFF
REORDER_PROBABILITY = 0.05   # 5%  — adjacent chunks swapped (out-of-order)

# Storage
SERVER_STORAGE_DIR = "server_storage"  # server saves uploads here
CLIENT_OUTPUT_DIR  = "client_output"   # clients save received files here

# Logging
LOG_LEVEL = "INFO"   # change to DEBUG for full chunk-level logs
```

### Enabling fault simulation

```python
# config.py
SIMULATE_ERRORS     = True
DROP_PROBABILITY    = 0.10
CORRUPT_PROBABILITY = 0.05
REORDER_PROBABILITY = 0.10
MAX_RETRIES         = 10
```

Then run `python main.py`. You will see all three fault types being caught and recovered:

```
WARNING  server.session — chunk 3 dropped (attempt 1)
WARNING  client.client — timeout waiting for chunk, sent NACK
WARNING  server.session — chunk 3 NACK'd (attempt 1/10), retransmitting
WARNING  client.client — NACK chunk 5 (attempt 1/10)     ← CRC mismatch
WARNING  server.session — chunk 5 NACK'd (attempt 1/10), retransmitting
WARNING  server.session — got ACK for seq 7, expected 8. Retransmitting  ← reorder
...
INFO     client.client — checksum verified ✓
INFO     client.client — Transfer Successful ✓
```

---

## Running Tests

```bash
# Full suite
python -m pytest tests/ --asyncio-mode=auto -v

# Individual files
python -m pytest tests/test_protocol.py --asyncio-mode=auto -v
python -m pytest tests/test_transfer.py --asyncio-mode=auto -v
python -m pytest tests/test_concurrency.py --asyncio-mode=auto -v

# Stop on first failure
python -m pytest tests/ --asyncio-mode=auto -x

# Filter by keyword
python -m pytest tests/ --asyncio-mode=auto -k "corruption"

# Show slowest tests
python -m pytest tests/ --asyncio-mode=auto --durations=5
```

### Test results

```
46 passed in 4.64s
```

| File | Tests | What is covered |
|---|---|---|
| `test_protocol.py` | 22 | Wire format, CRC32, SHA-256, chunk splitting, ACK/NACK round-trips |
| `test_transfer.py` | 8 | Single client: 1B, 1KB, 1MB, empty file, binary data, drop simulation |
| `test_concurrency.py` | 16 | 2/5/10/20 concurrent clients, data isolation, failure isolation, corruption under load, out-of-order delivery, throughput baseline |

---

## Verifying File Integrity Manually

After a transfer, output files are in `client_output/`. Verify with your OS tools:

**Windows (PowerShell):**
```powershell
Get-FileHash client_output\client_1_myfile.pdf -Algorithm SHA256
Get-FileHash myfile.pdf -Algorithm SHA256
# Both hashes must match exactly
```

**Linux / macOS:**
```bash
sha256sum client_output/client_1_myfile.pdf myfile.pdf
# Both hashes must match exactly
```

---

## How It Works — End to End

```
Client                                     Server
──────                                     ──────
read file from disk
send UPLOAD_REQUEST  ──────────────────►
  (opcode + size + raw bytes)              receive file
                                           save to server_storage/
                                           split into 64 KB chunks
                                           optionally reorder chunks (simulation)
                                           compute SHA-256 of full file

                      ◄──────────────────  send CHUNK_DATA(seq=0)
                                             header: crc32 of clean payload
                                             payload: bytes 0–65535
validate CRC32
store payload[seq_num=0]
send ACK(seq=0)       ──────────────────►
                                           confirmed seq=0 ✓, advance
                      ◄──────────────────  send CHUNK_DATA(seq=1)
...repeat for all chunks...

                      ◄──────────────────  send TRANSFER_DONE + SHA-256 digest

reassemble chunks in seq_num order
compute SHA-256 of reassembled bytes
compare with server digest               ← mismatch → error
save to client_output/
print: Transfer Successful ✓
```

---

## Bugs Fixed During Development

| Bug | Root Cause | Fix |
|---|---|---|
| Missing chunks under load | Protocol was positional — server and client drifted out of sync on retransmission | Every ACK/NACK carries explicit `seq_num`; server only advances when `ACK.seq_num == current_chunk.seq_num` |
| Corruption undetected | CRC computed after `maybe_corrupt()` ran, so header CRC matched the corrupted payload | CRC computed from clean payload first; corrupted payload spliced in after the header so client detects mismatch |
| Large files failing / 200s transfers | `CHUNK_SIZE=1024` produced thousands of chunks; 5% drop rate × 2s timeout accumulated to hundreds of seconds | `CHUNK_SIZE` raised to 65536 (64 KB), `RETRY_TIMEOUT` lowered to 0.5s, `MAX_RETRIES` raised to 10 |
| Windows `NotImplementedError` | `loop.add_signal_handler()` is Unix-only; Windows `ProactorEventLoop` raises `NotImplementedError` | Guarded with `if sys.platform != "win32"` |
| Config bleed between tests | Tests mutated global `config` values without restoring them, affecting subsequent tests | Snapshot all changed values at test start, restore unconditionally in `finally` |

---

## Performance

Tested on Windows with Python 3.13, localhost transfers:

| Scenario | Result |
|---|---|
| 3 large PDFs (~25 MB total) | 3/3 in 0.96s |
| 50 clients × 500 KB | 50/50 in 32s |
| **100 clients × 1 MB (100 MB total)** | **100/100 in 3.43s @ 29.13 MB/s** |

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.10+ |
| Networking | `asyncio.StreamReader` / `StreamWriter` |
| Concurrency | AsyncIO event loop (single-threaded, coroutine-based) |
| Binary framing | `struct` — big-endian packed headers |
| Integrity (file) | `hashlib.sha256` |
| Integrity (chunk) | `zlib.crc32` |
| Testing | `pytest` + `pytest-asyncio` |
| Dependencies | None (standard library only) |

---

## License

MIT
