"""SQLite WAL message persistence for AgentGate Gateway."""

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_INBOUND = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id TEXT PRIMARY KEY,
    received_at TEXT,
    delivered_at TEXT,
    processed_at TEXT,
    channel_type TEXT,
    channel_bot_id TEXT DEFAULT '',
    chat_id TEXT DEFAULT '',
    group_name TEXT DEFAULT '',
    sender_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    content TEXT,
    backend_id TEXT,
    delivery_status TEXT DEFAULT 'pending',
    process_status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    dedup_key TEXT UNIQUE
)
"""

_CREATE_OUTBOUND = """
CREATE TABLE IF NOT EXISTS outbound_messages (
    id TEXT PRIMARY KEY,
    fetched_at TEXT,
    pushed_at TEXT,
    backend_id TEXT,
    channel_type TEXT,
    chat_id TEXT,
    group_name TEXT DEFAULT '',
    content TEXT,
    push_status TEXT DEFAULT 'pending',
    shard_index INTEGER DEFAULT 1,
    shard_total INTEGER DEFAULT 1,
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
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
    "CREATE INDEX IF NOT EXISTS idx_inbound_delivery_status ON inbound_messages(delivery_status)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_process_status ON inbound_messages(process_status)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_backend_id ON inbound_messages(backend_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_push_status ON outbound_messages(push_status)",
    "CREATE INDEX IF NOT EXISTS idx_outbound_backend_id ON outbound_messages(backend_id)",
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
        await self._conn.execute(_CREATE_INBOUND)
        await self._conn.execute(_CREATE_OUTBOUND)
        await self._conn.execute(_CREATE_POLL_OFFSETS)
        for idx_sql in _CREATE_INDEXES:
            await self._conn.execute(idx_sql)
        await self._conn.commit()
        logger.info("MessageDB initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- inbound ----

    async def save_inbound(self, msg: dict) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            INSERT INTO inbound_messages (
                id, received_at, delivered_at, processed_at,
                channel_type, channel_bot_id, chat_id, group_name,
                sender_id, sender_name, content, backend_id,
                delivery_status, process_status, retry_count,
                error_message, dedup_key
            ) VALUES (
                :id, :received_at, :delivered_at, :processed_at,
                :channel_type, :channel_bot_id, :chat_id, :group_name,
                :sender_id, :sender_name, :content, :backend_id,
                :delivery_status, :process_status, :retry_count,
                :error_message, :dedup_key
            )
            """,
            {
                "id": msg.get("id"),
                "received_at": msg.get("received_at"),
                "delivered_at": msg.get("delivered_at"),
                "processed_at": msg.get("processed_at"),
                "channel_type": msg.get("channel_type"),
                "channel_bot_id": msg.get("channel_bot_id", ""),
                "chat_id": msg.get("chat_id", ""),
                "group_name": msg.get("group_name", ""),
                "sender_id": msg.get("sender_id", ""),
                "sender_name": msg.get("sender_name", ""),
                "content": msg.get("content"),
                "backend_id": msg.get("backend_id"),
                "delivery_status": msg.get("delivery_status", "pending"),
                "process_status": msg.get("process_status", "pending"),
                "retry_count": msg.get("retry_count", 0),
                "error_message": msg.get("error_message"),
                "dedup_key": msg.get("dedup_key"),
            },
        )
        await self._conn.commit()

    async def update_inbound_delivery(
        self,
        msg_id: str,
        status: str,
        delivered_at: str | None = None,
        error_message: str | None = None,
    ) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            UPDATE inbound_messages
            SET delivery_status = ?,
                delivered_at = COALESCE(?, delivered_at),
                error_message = COALESCE(?, error_message)
            WHERE id = ?
            """,
            (status, delivered_at, error_message, msg_id),
        )
        await self._conn.commit()

    async def update_inbound_process(
        self,
        msg_id: str,
        status: str,
        processed_at: str | None = None,
    ) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            UPDATE inbound_messages
            SET process_status = ?,
                processed_at = COALESCE(?, processed_at)
            WHERE id = ?
            """,
            (status, processed_at, msg_id),
        )
        await self._conn.commit()

    async def get_pending_inbound(self) -> list[dict]:
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT * FROM inbound_messages WHERE delivery_status = 'pending' ORDER BY received_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_unprocessed_for_backend(self, backend_id: str) -> list[dict]:
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            """
            SELECT * FROM inbound_messages
            WHERE backend_id = ?
              AND delivery_status = 'delivered'
              AND process_status IN ('pending', 'unprocessed_crash')
            ORDER BY received_at
            """,
            (backend_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_failed_inbound_for_backend(self, backend_id: str) -> list[dict]:
        """Get recently-failed inbound messages for a backend (for recovery on backend restore)."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            """
            SELECT * FROM inbound_messages
            WHERE backend_id = ?
              AND delivery_status = 'failed'
            ORDER BY received_at
            """,
            (backend_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def increment_inbound_retry(self, msg_id: str) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            "UPDATE inbound_messages SET retry_count = retry_count + 1 WHERE id = ?",
            (msg_id,),
        )
        await self._conn.commit()

    async def has_dedup_key(self, key: str) -> bool:
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT 1 FROM inbound_messages WHERE dedup_key = ? LIMIT 1", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    # ---- outbound ----

    async def save_outbound(self, msg: dict) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            INSERT INTO outbound_messages (
                id, fetched_at, pushed_at, backend_id,
                channel_type, chat_id, group_name, content,
                push_status, shard_index, shard_total,
                retry_count, error_message, content_hash
            ) VALUES (
                :id, :fetched_at, :pushed_at, :backend_id,
                :channel_type, :chat_id, :group_name, :content,
                :push_status, :shard_index, :shard_total,
                :retry_count, :error_message, :content_hash
            )
            """,
            {
                "id": msg.get("id"),
                "fetched_at": msg.get("fetched_at"),
                "pushed_at": msg.get("pushed_at"),
                "backend_id": msg.get("backend_id"),
                "channel_type": msg.get("channel_type"),
                "chat_id": msg.get("chat_id"),
                "group_name": msg.get("group_name", ""),
                "content": msg.get("content"),
                "push_status": msg.get("push_status", "pending"),
                "shard_index": msg.get("shard_index", 1),
                "shard_total": msg.get("shard_total", 1),
                "retry_count": msg.get("retry_count", 0),
                "error_message": msg.get("error_message"),
                "content_hash": msg.get("content_hash"),
            },
        )
        await self._conn.commit()

    async def update_outbound_push(
        self,
        msg_id: str,
        status: str,
        pushed_at: str | None = None,
        error_message: str | None = None,
    ) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            """
            UPDATE outbound_messages
            SET push_status = ?,
                pushed_at = COALESCE(?, pushed_at),
                error_message = COALESCE(?, error_message)
            WHERE id = ?
            """,
            (status, pushed_at, error_message, msg_id),
        )
        await self._conn.commit()

    async def get_pending_outbound(self) -> list[dict]:
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT * FROM outbound_messages WHERE push_status = 'pending' ORDER BY fetched_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def increment_outbound_retry(self, msg_id: str) -> None:
        assert self._conn is not None, "DB not initialized"
        await self._conn.execute(
            "UPDATE outbound_messages SET retry_count = retry_count + 1 WHERE id = ?",
            (msg_id,),
        )
        await self._conn.commit()

    async def has_outbound_content_hash(self, backend_id: str, content_hash: str) -> bool:
        """Check if an outbound message with this content_hash already exists for this backend."""
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT 1 FROM outbound_messages WHERE backend_id = ? AND content_hash = ? LIMIT 1",
            (backend_id, content_hash),
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def get_failed_outbound(self) -> list[dict]:
        assert self._conn is not None, "DB not initialized"
        async with self._conn.execute(
            "SELECT * FROM outbound_messages WHERE push_status = 'failed' ORDER BY fetched_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---- query API (F13) ----

    async def query_messages(
        self, filters: dict
    ) -> tuple[list[dict], int]:
        assert self._conn is not None, "DB not initialized"

        direction = filters.get("direction")

        # E-6: When direction is omitted, query both tables and merge
        if direction is None:
            inbound_filters = {**filters, "direction": "inbound"}
            outbound_filters = {**filters, "direction": "outbound"}
            in_rows, in_total = await self._query_single_direction(inbound_filters)
            out_rows, out_total = await self._query_single_direction(outbound_filters)
            # Merge and re-paginate
            all_rows = in_rows + out_rows
            total = in_total + out_total
            return all_rows, total

        return await self._query_single_direction(filters)

    async def _query_single_direction(
        self, filters: dict
    ) -> tuple[list[dict], int]:
        """Query a single direction (inbound or outbound)."""
        assert self._conn is not None, "DB not initialized"

        direction = filters.get("direction", "inbound")
        if direction == "outbound":
            table = "outbound_messages"
        else:
            table = "inbound_messages"

        conditions: list[str] = []
        params: list = []

        start_time = filters.get("start_time")
        end_time = filters.get("end_time")
        channel_type = filters.get("channel_type")
        backend_id = filters.get("backend_id")

        if direction == "inbound":
            time_col = "received_at"
            status_col_delivery = filters.get("delivery_status")
            status_col_process = filters.get("process_status")
            if status_col_delivery:
                conditions.append("delivery_status = ?")
                params.append(status_col_delivery)
            if status_col_process:
                conditions.append("process_status = ?")
                params.append(status_col_process)
        else:
            time_col = "fetched_at"
            push_status = filters.get("push_status")
            if push_status:
                conditions.append("push_status = ?")
                params.append(push_status)

        if start_time:
            conditions.append(f"{time_col} >= ?")
            params.append(start_time)
        if end_time:
            conditions.append(f"{time_col} <= ?")
            params.append(end_time)
        if channel_type:
            conditions.append("channel_type = ?")
            params.append(channel_type)
        if backend_id:
            conditions.append("backend_id = ?")
            params.append(backend_id)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Count
        count_sql = f"SELECT COUNT(*) FROM {table} {where}"
        async with self._conn.execute(count_sql, params) as cursor:
            count_row = await cursor.fetchone()
        total = count_row[0] if count_row else 0

        # Pagination
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))
        offset = (page - 1) * page_size

        data_sql = f"SELECT * FROM {table} {where} ORDER BY {time_col} LIMIT ? OFFSET ?"
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
