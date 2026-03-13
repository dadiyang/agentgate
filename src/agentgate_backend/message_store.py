"""In-memory message ID store for inject idempotency. TTL-based cleanup."""
import threading
import time


class MessageStore:
    """Track injected message IDs to prevent duplicate send_keys.

    Thread-safe. Expired entries (older than ttl seconds) are lazily cleaned
    up on each has() call so the store never grows without bound.
    """

    def __init__(self, ttl: int = 3600) -> None:
        self._store: dict[str, float] = {}  # message_id -> timestamp
        self._ttl = ttl
        self._lock = threading.Lock()

    def has(self, message_id: str) -> bool:
        """Return True if message_id was previously added and has not expired."""
        with self._lock:
            self._cleanup()
            return message_id in self._store

    def add(self, message_id: str) -> None:
        """Record a message_id with the current timestamp."""
        with self._lock:
            self._store[message_id] = time.time()

    def remove(self, message_id: str) -> None:
        """Remove a message_id (idempotent — no error if not present)."""
        with self._lock:
            self._store.pop(message_id, None)

    def _cleanup(self) -> None:
        """Remove entries older than ttl. Caller must hold the lock."""
        cutoff = time.time() - self._ttl
        self._store = {k: v for k, v in self._store.items() if v > cutoff}
