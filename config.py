# =============================================================================
# config.py — Central configuration for the file transfer system.
# All tuneable parameters live here. Nothing is hardcoded elsewhere.
# =============================================================================

# --- Network ---
HOST: str    = "127.0.0.1"
PORT: int    = 9000
BACKLOG: int = 50              # Max queued connections before the OS drops them

# --- Protocol ---
# CHUNK_SIZE directly controls transfer speed for large files.
# Larger chunks = fewer round-trips = fewer timeout opportunities = faster.
#
#   File size | 1 KB chunks | 8 KB chunks | 64 KB chunks
#   ----------+-------------+-------------+-------------
#     1 MB    |  1,024      |    128      |    16
#     8 MB    |  8,192      |  1,024      |   128
#   100 MB    | 102,400     | 12,800      |  1,600
#
# At 1 KB chunks a 7.8 MB file = 7,988 chunks.
# With 5% drop rate that is ~400 timeout events x 0.5s = 200s wasted.
# At 64 KB chunks the same file = 125 chunks -> ~6 timeouts -> ~3s wasted.
#
CHUNK_SIZE: int    = 65536          # 64 KB -- fast for large files, fine for small ones
HEADER_FORMAT: str = "!I I I ? I"  # client_id, seq_num, total_chunks, is_last, crc32
HEADER_SIZE: int   = 17            # struct.calcsize(HEADER_FORMAT)

# --- Reliability ---
# MAX_RETRIES must survive the worst-case consecutive-drop streak.
# With 5% drop rate: P(10 consecutive drops) is negligible for any file size.
MAX_RETRIES: int      = 10          # retries per chunk before giving up
RETRY_TIMEOUT: float  = 0.5         # seconds to wait for ACK before sending NACK
SOCKET_TIMEOUT: float = 5.0         # general socket read timeout

# --- Concurrency ---
MAX_WORKER_THREADS: int = 20        # ThreadPoolExecutor ceiling (file I/O only)

# --- Error Simulation ---
# Set SIMULATE_ERRORS = False for clean transfers with real files.
# Enable only when testing retransmission behaviour.
SIMULATE_ERRORS: bool      = False  # master switch -- False = clean transfers
DROP_PROBABILITY: float    = 0.05   # 5%  -- chunk silently dropped
CORRUPT_PROBABILITY: float = 0.03   # 3%  -- one payload byte XOR'd with 0xFF
REORDER_PROBABILITY: float = 0.05  # 5%  -- adjacent chunks swapped (out-of-order simulation)

# --- Filesystem ---
SERVER_STORAGE_DIR: str = "server_storage"  # server saves uploads here
CLIENT_OUTPUT_DIR: str  = "client_output"   # clients save received files here

# --- Logging ---
LOG_LEVEL: str       = "INFO"
LOG_FORMAT: str      = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_DATE_FORMAT: str = "%H:%M:%S"