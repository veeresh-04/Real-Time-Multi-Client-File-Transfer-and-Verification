# Multi-Client File Transfer System

A production-grade, async multi-client file transfer system built in Python using **AsyncIO** and **TCP sockets**. The server handles concurrent client sessions independently, splits files into chunks, verifies integrity with SHA-256 checksums and per-chunk CRC32, and implements full retransmission logic for simulated network faults.

---

## Features

- **Async TCP server** — single event loop handles unlimited concurrent clients with no thread overhead
- **Chunked transfer** — files split into 1024-byte chunks, each tagged with sequence number, client ID, and CRC32
- **End-to-end integrity** — SHA-256 checksum verified after full reassembly; CRC32 verified per chunk
- **Retransmission protocol** — seq_num-driven ACK/NACK; server retransmits exactly the chunk requested
- **Network fault simulation** — configurable packet drop and payload corruption rates
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
│   ├── protocol.py            # Wire format, chunk struct, ACK/NACK, CRC32, helpers
│   ├── checksum.py            # SHA-256 file integrity
│   └── errors.py              # Typed exception hierarchy
│
├── server/
│   ├── server.py              # Async TCP listener, connection dispatch
│   └── session.py             # Per-client session — upload, chunk, deliver, verify
│
├── client/
│   └── client.py              # Upload, receive chunks, ACK/NACK, reassemble, verify
│
├── simulation/
│   └── network.py             # Packet drop and payload corruption injection
│
├── tests/
│   ├── test_protocol.py       # 22 unit tests — wire format, CRC, checksums
│   ├── test_transfer.py       #  8 integration tests — single client, error sim
│   └── test_concurrency.py   # 14 concurrency tests — isolation, stress, performance
│
├── main.py                    # Entry point — demo or pass your own files
└── stress_test.py             # N-client stress test with throughput reporting
```

---

## Wire Protocol

Every byte on the wire is defined in `core/protocol.py`. The protocol is binary, big-endian (network byte order).

### Opcodes (1 byte)

| Opcode | Hex  | Direction         | Meaning                        |
|--------|------|-------------------|--------------------------------|
| UPLOAD_REQUEST | `0x01` | client → server | Start upload with file size |
| CHUNK_DATA     | `0x02` | server → client | Deliver one file chunk      |
| ACK            | `0x03` | client → server | Chunk received OK           |
| NACK           | `0x04` | client → server | Chunk failed, resend        |
| TRANSFER_DONE  | `0x05` | server → client | All chunks sent + checksum  |
| ERROR          | `0x06` | both directions | Session-level error         |

### UPLOAD_REQUEST layout

```
1B   0x01         opcode
4B   file_size    uint32, network order
NB   file_data    raw bytes
```

### CHUNK_DATA layout

```
1B   0x02         opcode
2B   payload_len  uint16, network order
──── chunk header (17 bytes) ────────────────
4B   client_id    uint32
4B   seq_num      uint32   (zero-based)
4B   total_chunks uint32
1B   is_last      bool
4B   crc32        uint32   CRC of the CLEAN payload (corruption detection)
─────────────────────────────────────────────
NB   payload      raw bytes (≤ 1024)
```

### ACK / NACK layout

```
1B   0x03 or 0x04   opcode
4B   client_id      uint32
4B   seq_num        uint32   (the exact chunk being acknowledged)
```

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
                                    CRC check passes
              ◄──────────────────  ACK(seq_num=N)
send chunk N+1 ─────────────────►
                                    ...

── on drop ──────────────────────────────────────────
send chunk N  ──────────────── ✗   (dropped)
              ◄──────────────────  NACK(seq_num=0xFFFFFFFF)  [timeout]
send chunk N  ──────────────────►  (retransmit clean original)
                                    ACK(seq_num=N)

── on corruption ────────────────────────────────────
send chunk N  ──────────────────►
                                    CRC mismatch detected
              ◄──────────────────  NACK(seq_num=N)
send chunk N  ──────────────────►  (retransmit clean original)
                                    CRC passes → ACK(seq_num=N)
```

The CRC in the chunk header is always computed from the **clean payload** before simulation faults are applied. If corruption is simulated, the header CRC reflects what the payload _should_ be — the client detects the mismatch immediately.

---

## Installation

```bash
git clone https://github.com/veeresh-04/Real-Time-Multi-Client-File-Transfer-and-Verification.git
cd file_transfer

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

**Requires Python 3.10+** (uses `match` patterns and `X | Y` union type hints).

---

## Usage

### Built-in demo — 3 concurrent clients

```bash
python main.py
```

Creates three sample files and transfers them concurrently. Output:

```
============================================================
  Transfer Summary   (1.38s)
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

Each file is handled by its own client. All run concurrently.

### Separate terminals (real multi-client feel)

**Terminal 1 — server:**
```bash
python -c "
import asyncio, logging, config
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s', datefmt='%H:%M:%S')
from server.server import FileTransferServer
asyncio.run(FileTransferServer().serve_forever())
"
```

**Terminal 2 — Client A:**
```bash
python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s', datefmt='%H:%M:%S')
from client.client import FileTransferClient
result = asyncio.run(FileTransferClient(client_id=1, file_path='main.py').run())
print('Result:', 'SUCCESS' if result else 'FAILED')
"
```

**Terminal 3 — Client B (at the same time):**
```bash
python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s', datefmt='%H:%M:%S')
from client.client import FileTransferClient
result = asyncio.run(FileTransferClient(client_id=2, file_path='config.py').run())
print('Result:', 'SUCCESS' if result else 'FAILED')
"
```

### Stress test — N concurrent clients

```bash
python stress_test.py
```

Edit `stress_test.py` to control:

```python
NUM_CLIENTS  = 50     # number of concurrent clients
FILE_SIZE_KB = 500    # file size per client in KB
```

Output:
```
==================================================
  50/50 clients succeeded
  Total data: 24.4 MB
  Time: 32.76s
  Throughput: 0.75 MB/s
==================================================
```

---

## Configuration

All parameters live in `config.py`. Nothing is hardcoded anywhere else.

```python
# Network
HOST      = "127.0.0.1"
PORT      = 9000
BACKLOG   = 50           # max queued OS connections

# Protocol
CHUNK_SIZE = 1024        # bytes per chunk payload

# Reliability
MAX_RETRIES    = 5       # retries before giving up on a chunk
RETRY_TIMEOUT  = 0.5    # seconds to wait for ACK before sending NACK

# Error Simulation
SIMULATE_ERRORS    = True   # master on/off switch
DROP_PROBABILITY   = 0.05   # 5%  — chunk silently dropped
CORRUPT_PROBABILITY = 0.03  # 3%  — one byte in payload XOR'd with 0xFF

# Storage
SERVER_STORAGE_DIR = "server_storage"  # server saves uploads here
CLIENT_OUTPUT_DIR  = "client_output"   # clients save received files here

# Logging
LOG_LEVEL = "INFO"   # DEBUG for full chunk-level logs
```

### Seeing retransmission live

Set in `config.py`:
```python
SIMULATE_ERRORS     = True
DROP_PROBABILITY    = 0.20   # 20% drops
CORRUPT_PROBABILITY = 0.10   # 10% corruption
MAX_RETRIES         = 10
```

Then run `python main.py`. You will see:
```
WARNING  server.session — chunk 21 dropped (attempt 1)
WARNING  client.client — timeout waiting for chunk, sent NACK
WARNING  server.session — chunk 21 NACK'd (attempt 1/10), retransmitting
WARNING  client.client — NACK chunk 34 (attempt 1/10)    ← CRC mismatch
WARNING  server.session — chunk 34 NACK'd (attempt 1/10), retransmitting
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
python -m pytest tests/ --asyncio-mode=auto -v -x

# Filter by keyword
python -m pytest tests/ --asyncio-mode=auto -v -k "corruption"

# Show slowest tests
python -m pytest tests/ --asyncio-mode=auto -v --durations=5
```

### Test results

```
44 passed in 6.86s
```

| File | Tests | Coverage |
|---|---|---|
| `test_protocol.py` | 22 | Wire format, CRC32, SHA-256, chunk splitting, ACK/NACK round-trips |
| `test_transfer.py` | 8  | Single client: 1B, 1KB, 1MB, empty file, binary data, drop simulation |
| `test_concurrency.py` | 14 | 2/5/10/20 concurrent clients, data isolation, failure isolation, corruption under load, throughput baseline |

---

## Verifying File Integrity Manually

After a transfer, output files are in `client_output/`. Verify with your OS:

**Windows (PowerShell):**
```powershell
Get-FileHash client_output\client_1_myfile.txt -Algorithm SHA256
Get-FileHash myfile.txt -Algorithm SHA256
# Both hashes must match
```

**Linux / macOS:**
```bash
sha256sum client_output/client_1_myfile.txt myfile.txt
# Both hashes must match
```

---

## How It Works — End to End

```
Client                                    Server
──────                                    ──────
read file from disk
send UPLOAD_REQUEST  ──────────────────►
  (opcode + size + raw bytes)             receive file
                                          save to server_storage/
                                          split into 1024B chunks
                                          compute SHA-256 of full file

                     ◄──────────────────  send CHUNK_DATA(0)
                                            header: client_id, seq=0,
                                            total, is_last, crc32
                                            payload: bytes 0–1023
validate CRC32
store payload[0]
send ACK(seq=0)      ──────────────────►
                                          confirmed seq=0 ✓
                     ◄──────────────────  send CHUNK_DATA(1)
...repeat for all chunks...

                     ◄──────────────────  send TRANSFER_DONE
                                            + SHA-256 hex digest

reassemble chunks in seq order
compute SHA-256 of reassembled file
compare with server's digest             ← mismatch → error
save to client_output/
print: Transfer Successful ✓
```

---

## Bugs Fixed During Development

| Bug | Root Cause | Fix |
|---|---|---|
| Missing chunks under load | Protocol was positional, not seq_num-driven — server and client drifted out of sync during retransmission | Every ACK/NACK now carries explicit `seq_num`; server only advances when `ACK.seq_num == current_chunk.seq_num` |
| Corruption not detected | CRC was computed after `maybe_corrupt()` ran, so header CRC matched the corrupted payload | CRC computed from clean payload before simulation; corrupted payload spliced in after the header |
| 78-second stress test | `RETRY_TIMEOUT=2.0s` × 50 clients × multiple drops = massive accumulated wait | Reduced to `0.5s` — fast enough to catch real drops without stalling under load |
| Windows `NotImplementedError` | `loop.add_signal_handler()` is Unix-only; Windows `ProactorEventLoop` raises `NotImplementedError` | Guarded with `if sys.platform != "win32"` |
| Config bleed between tests | Tests mutated global `config` values without restoring them in `finally` | Snapshot all values before mutation, restore unconditionally in `finally` |

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