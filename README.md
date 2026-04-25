# Async File Transfer System

A Python asyncio-based client/server file transfer system with:

- Chunked transfer protocol
- ACK/NACK-based reliability with retries
- CRC32 validation per chunk
- End-to-end SHA-256 verification
- Fault simulation for dropped/corrupted chunks
- Concurrency and integration test coverage

## Features

- Asynchronous TCP server handling multiple clients concurrently
- Per-client session isolation (one client failure does not stop others)
- Retransmission on timeout, drop, or corruption via NACK
- Output persistence for server-side received files and client-side reconstructed files
- Configurable network/protocol/retry/simulation parameters in config.py

## Project Layout

- main.py: demo entry point, starts server and runs clients
- config.py: central settings (host, port, chunk size, retries, simulation, logging)
- server/server.py: async TCP server
- server/session.py: per-client transfer lifecycle
- client/client.py: upload, chunk receive, ACK/NACK, checksum verify
- core/protocol.py: opcodes, chunk format, ACK/NACK messages, chunk splitting
- core/checksum.py: SHA-256 helpers
- simulation/network.py: packet drop/corruption simulation
- tests/: protocol, transfer, and concurrency/performance tests

## Protocol Summary

Message opcodes:

- 0x01 UPLOAD_REQUEST (client -> server)
- 0x02 CHUNK_DATA (server -> client)
- 0x03 ACK (both directions)
- 0x04 NACK (client -> server)
- 0x05 TRANSFER_DONE (server -> client)
- 0x06 ERROR (both directions)

Chunk format (header + payload):

- Header fields: client_id, seq_num, total_chunks, is_last, crc32
- Header size: 17 bytes (network byte order)
- Payload size: up to CHUNK_SIZE (default 1024)

## Requirements

- Python 3.10+ (tested in this workspace on Python 3.13)
- pip

## Setup

### Windows PowerShell

```powershell
cd C:\Users\veere\file_transfer
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install pytest pytest-asyncio
```

If activation is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### macOS/Linux

```bash
cd /path/to/file_transfer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install pytest pytest-asyncio
```

## Run the Demo

From project root:

```bash
python main.py
```

This starts the server, creates demo files when no arguments are passed, runs multiple clients concurrently, and prints a transfer summary.

Run with specific files:

```bash
python main.py path/to/file1.txt path/to/file2.bin
```

## Run Tests

```bash
python -m pytest tests --asyncio-mode=auto -v
```

Quick mode:

```bash
python -m pytest tests --asyncio-mode=auto -q
```

## Configuration

Tune behavior in config.py:

- Network: HOST, PORT, BACKLOG
- Protocol: CHUNK_SIZE
- Reliability: MAX_RETRIES, RETRY_TIMEOUT, SOCKET_TIMEOUT
- Fault simulation: SIMULATE_ERRORS, DROP_PROBABILITY, CORRUPT_PROBABILITY
- Paths: SERVER_STORAGE_DIR, CLIENT_OUTPUT_DIR
- Logging: LOG_LEVEL, LOG_FORMAT

Example clean run values:

- SIMULATE_ERRORS = False
- MAX_RETRIES = 5

Example fault-injection run values:

- SIMULATE_ERRORS = True
- DROP_PROBABILITY = 0.05
- CORRUPT_PROBABILITY = 0.03

## Output Locations

- Server writes uploaded binaries to server_storage/
- Clients write reconstructed files to client_output/

## Git Ignore

Use the root .gitignore to avoid committing generated artifacts:

- .venv/
- __pycache__/
- .pytest_cache/
- client_output/
- server_storage/

## Troubleshooting

### Pylance shows "Import pytest could not be resolved"

- Ensure pytest is installed in the same environment selected in VS Code.
- Select interpreter: .venv/Scripts/python.exe (Windows) or .venv/bin/python (macOS/Linux).
- Reload the VS Code window after selecting the interpreter.

### Port already in use

- Change PORT in config.py or ensure no previous server process is still running.

### Tests pass in terminal but editor still shows warnings

- VS Code is likely using a different interpreter than the terminal.
- Re-select the interpreter for this workspace and reload window.

## License

Add your preferred license file if you plan to distribute this repository publicly.
