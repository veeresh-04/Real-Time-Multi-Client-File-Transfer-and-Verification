# =============================================================================
# core/errors.py — Custom exception hierarchy for the file transfer system.
#
# Why custom exceptions?
#   - Callers can catch specific failure modes, not bare Exception.
#   - Every failure has a clear name that reads like documentation.
#   - Layered hierarchy lets you catch broad or narrow as needed.
# =============================================================================


class FileTransferError(Exception):
    """Base exception for all file-transfer failures.

    Catch this to handle any error from this system without caring
    about the specific cause.
    """


# --- Protocol Errors ----------------------------------------------------------

class ProtocolError(FileTransferError):
    """Raised when a message violates the wire protocol.

    Examples: wrong magic bytes, header too short, unrecognised opcode.
    """


class ChecksumMismatchError(FileTransferError):
    """Raised when the reassembled file's SHA-256 does not match the server's.

    Carries both digests so the caller can log them for diagnostics.
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Checksum mismatch — expected {expected[:12]}…, got {actual[:12]}…"
        )


# --- Transfer Errors ----------------------------------------------------------

class ChunkError(FileTransferError):
    """Raised when a specific chunk cannot be delivered after all retries.

    Attributes:
        seq_num: The zero-based sequence number of the offending chunk.
    """

    def __init__(self, seq_num: int, reason: str) -> None:
        self.seq_num = seq_num
        super().__init__(f"Chunk {seq_num} failed: {reason}")


class MaxRetriesExceededError(FileTransferError):
    """Raised when a chunk has been retried MAX_RETRIES times without success."""

    def __init__(self, seq_num: int, retries: int) -> None:
        self.seq_num = seq_num
        self.retries = retries
        super().__init__(
            f"Chunk {seq_num} failed after {retries} retries — giving up."
        )


# --- Session Errors -----------------------------------------------------------

class SessionError(FileTransferError):
    """Raised for lifecycle problems in a client session.

    Examples: client disconnected mid-transfer, duplicate client ID.
    """


class ClientDisconnectedError(SessionError):
    """Raised when the remote end closes the connection unexpectedly."""

    def __init__(self, client_id: int) -> None:
        self.client_id = client_id
        super().__init__(f"Client {client_id} disconnected unexpectedly.")