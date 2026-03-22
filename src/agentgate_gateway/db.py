"""SQLite WAL message persistence for AgentGate Gateway.

Unified `messages` table with direction field (inbound/outbound).
Status: pending → delivered → failed (3 symmetric states).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    backend_id TEXT,
    channel_type TEXT,
    channel_bot_id TEXT DEFAULT '',
    chat_id TEXT DEFAULT '',
    group_name TEXT DEFAULT '',
    sender_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    content TEXT,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    dedup_key TEXT,
    shard_index INTEGER DEFAULT 1,
    shard_total INTEGER DEFAULT 1,
    content_hash TEXT
)
"""

_CREATE_POLL_OFFSETS = """
CREATE TABLE IF NOT EXISTS poll_offsets (
    backend_id TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_msg_direction ON messages(direction)",
    "CREATE INDEX IF NOT EXISTS idx_msg_status ON messages(status)",
    "CREATE INDEX IF NOT EXISTS idx_msg_backend_id ON messages(backend_id)",
    "CREATE INDEX IF NOT EXISTS idx_msg_timestamp ON messages(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_msg_backend_ts ON messages(backend_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_msg_backend_hash ON messages(backend_id, content_hash)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup ON messages(dedup_key) WHERE dedup_key IS NOT NULL",
]


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


class MessageDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute(_CREATE_MESSAGES)
        await self._conn.execute(_CREATE_POLL_OFFSETS)
        for idx_sql in _CREATE_INDEXES:
            await self._conn.execute(idx_sql)
        await self._conn.commit()
        logger.info("MessageDB initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- save ----

    async def save_inbound(self, msg: dict) -> None:
        """Persist an inbound message."""
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            INSERT INTO messages (
                id, direction, timestamp, backend_id,
                channel_type, channel_bot_id, chat_id, group_name,
                sender_id, sender_name, content, status,
                retry_count, error_message, dedup_key
            ) VALUES (
                :id, 'inbound', :timestamp, :backend_id,
                :channel_type, :channel_bot_id, :chat_id, :group_name,
                :sender_id, :sender_name, :content, 'pending',
                0, NULL, :dedup_key
            )
            """,
            {
                "id": msg.get("id"),
                "timestamp": msg.get("received_at") or msg.get("timestamp"),
                "backend_id": msg.get("backend_id"),
                "channel_type": msg.get("channel_type"),
                "channel_bot_id": msg.get("channel_bot_id", ""),
                "chat_id": msg.get("chat_id", ""),
                "group_name": msg.get("group_name", ""),
                "sender_id": msg.get("sender_id", ""),
                "sender_name": msg.get("sender_name", ""),
                "content": msg.get("content"),
                "dedup_key": msg.get("dedup_key"),
            },
        )
        await self._conn.commit()

    async def save_outbound(self, msg: dict) -> None:
        """Persist an outbound message."""
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            INSERT INTO messages (
                id, direction, timestamp, backend_id,
                channel_type, chat_id, group_name, content,
                status, shard_index, shard_total,
                retry_count, error_message, content_hash
            ) VALUES (
                :id, 'outbound', :timestamp, :backend_id,
                :channel_type, :chat_id, :group_name, :content,
                'pending', :shard_index, :shard_total,
                0, NULL, :content_hash
            )
            """,
            {
                "id": msg.get("id"),
                "timestamp": msg.get("fetched_at") or msg.get("timestamp"),
                "backend_id": msg.get("backend_id"),
                "channel_type": msg.get("channel_type"),
                "chat_id": msg.get("chat_id"),
                "group_name": msg.get("group_name", ""),
                "content": msg.get("content"),
                "shard_index": msg.get("shard_index", 1),
                "shard_total": msg.get("shard_total", 1),
                "content_hash": msg.get("content_hash"),
            },
        )
        await self._conn.commit()

    # ---- status updates ----

    async def update_status(
        self,
        msg_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update message status (pending/delivered/failed)."""
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            UPDATE messages
            SET status = ?,
                error_message = COALESCE(?, error_message)
            WHERE id = ?
            """,
            (status, error_message, msg_id),
        )
        await self._conn.commit()

    async def increment_retry(self, msg_id: str) -> None:
        """Increment retry_count for a message."""
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            "UPDATE messages SET retry_count = retry_count + 1 WHERE id = ?",
            (msg_id,),
        )
        await self._conn.commit()

    # ---- queries ----

    async def get_pending(self, direction: str) -> list[dict]:
        """Get pending messages for a direction (inbound/outbound)."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT * FROM messages WHERE direction = ? AND status = 'pending' ORDER BY timestamp",
            (direction,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_failed(self, direction: str, backend_id: str | None = None) -> list[dict]:
        """Get failed messages, optionally filtered by backend_id."""
        assert self._conn is not None, "DB not initialized"
        if backend_id:
            sql = "SELECT * FROM messages WHERE direction = ? AND status = 'failed' AND backend_id = ? ORDER BY timestamp"
            params = (direction, backend_id)
        else:
            sql = "SELECT * FROM messages WHERE direction = ? AND status = 'failed' ORDER BY timestamp"
            params = (direction,)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def has_dedup_key(self, key: str) -> bool:
        """Check if a message with this dedup_key exists."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT 1 FROM messages WHERE dedup_key = ? LIMIT 1", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def has_content_hash(self, backend_id: str, content_hash: str) -> bool:
        """Check if an outbound message with this content_hash exists for this backend."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT 1 FROM messages WHERE backend_id = ? AND content_hash = ? LIMIT 1",
            (backend_id, content_hash),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    # ---- query API ----

    async def query_messages(self, filters: dict) -> tuple[list[dict], int]:
        """Query messages with filters and pagination."""
        assert self._conn is not None, "DB not initialized"

        conditions: list[str] = []
        params: list = []

        direction = filters.get("direction")
        if direction:
            conditions.append("direction = ?")
            params.append(direction)

        status = filters.get("status")
        if status:
            conditions.append("status = ?")
            params.append(status)

        start_time = filters.get("start_time")
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)

        end_time = filters.get("end_time")
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        channel_type = filters.get("channel_type")
        if channel_type:
            conditions.append("channel_type = ?")
            params.append(channel_type)

        backend_id = filters.get("backend_id")
        if backend_id:
            conditions.append("backend_id = ?")
            params.append(backend_id)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Count
        count_sql = f"SELECT COUNT(*) FROM messages {where}"
        async with self._conn.execute(count_sql, params) as cursor:
            count_row = await cursor.fetchone()
        total = count_row[0] if count_row else 0

        # Pagination
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))
        offset = (page - 1) * page_size

        data_sql = f"SELECT * FROM messages {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        async with self._conn.execute(data_sql, params + [page_size, offset]) as cursor:
            rows = await cursor.fetchall()

        return [_row_to_dict(r) for r in rows], total

    # ---- poll offsets ----

    async def load_poll_offsets(self) -> dict[str, int]:
        """Load all persisted poll offsets. Returns {backend_id: byte_offset}."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute("SELECT backend_id, byte_offset FROM poll_offsets") as cursor:
            rows = await cursor.fetchall()
        return {row["backend_id"]: row["byte_offset"] for row in rows}

    async def save_poll_offset(self, backend_id: str, byte_offset: int) -> None:
        """Persist poll offset for a backend (upsert)."""
        assert self._conn is not None, "DB not initialized"
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO poll_offsets (backend_id, byte_offset, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(backend_id) DO UPDATE SET byte_offset = ?, updated_at = ?
            """,
            (backend_id, byte_offset, now, byte_offset, now),
        )
        await self._conn.commit()
