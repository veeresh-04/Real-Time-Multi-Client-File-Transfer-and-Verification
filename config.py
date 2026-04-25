# =============================================================================
# config.py — Central configuration for the file transfer system.
# All tuneable parameters live here. Nothing is hardcoded elsewhere.
# =============================================================================

# --- Network ---
HOST: str = "127.0.0.1"
PORT: int = 9000
BACKLOG: int = 50                  # Max queued connections before the OS drops them

# --- Protocol ---
CHUNK_SIZE: int = 1024             # Bytes per data chunk (payload only, not header)
HEADER_FORMAT: str = "!I I I ? I"  # network-order: client_id, seq_num, total_chunks, is_last, crc32
HEADER_SIZE: int = 17              # struct.calcsize(HEADER_FORMAT) — pre-computed for speed

# --- Reliability ---
MAX_RETRIES: int = 5               # How many times a client retries a failed chunk
RETRY_TIMEOUT: float = 2.0        # Seconds to wait for an ACK before retrying
SOCKET_TIMEOUT: float = 5.0       # General socket read timeout

# --- Concurrency ---
MAX_WORKER_THREADS: int = 20      # ThreadPoolExecutor ceiling

# --- Error Simulation ---
SIMULATE_ERRORS: bool = True       # Master switch — flip to False for clean runs
DROP_PROBABILITY: float = 0.05    # 5% chance a chunk is silently dropped
CORRUPT_PROBABILITY: float = 0.03 # 3% chance a chunk's payload is corrupted

# --- Filesystem ---
SERVER_STORAGE_DIR: str = "server_storage"   # Where server saves uploaded files
CLIENT_OUTPUT_DIR: str = "client_output"     # Where clients save received files

# --- Logging ---
LOG_LEVEL: str = "INFO"
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
LOG_DATE_FORMAT: str = "%H:%M:%S"