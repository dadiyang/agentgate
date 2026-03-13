"""Tests for agentgate_gateway.db"""

import pytest

from agentgate_gateway.db import MessageDB


def _make_inbound(
    msg_id: str = "msg-001",
    dedup_key: str = "dedup-001",
    backend_id: str = "fish-dev",
    channel_type: str = "feishu",
    delivery_status: str = "pending",
    process_status: str = "pending",
) -> dict:
    return {
        "id": msg_id,
        "received_at": "2024-01-01T10:00:00",
        "delivered_at": None,
        "processed_at": None,
        "channel_type": channel_type,
        "channel_bot_id": "bot1",
        "group_id": "grp1",
        "group_name": "Fish Dev Group",
        "sender_id": "user1",
        "sender_name": "Alice",
        "content": "Hello agent",
        "backend_id": backend_id,
        "delivery_status": delivery_status,
        "process_status": process_status,
        "retry_count": 0,
        "error_message": None,
        "dedup_key": dedup_key,
    }


def _make_outbound(
    msg_id: str = "out-001",
    backend_id: str = "fish-dev",
    channel_type: str = "feishu",
    push_status: str = "pending",
) -> dict:
    return {
        "id": msg_id,
        "fetched_at": "2024-01-01T10:01:00",
        "pushed_at": None,
        "backend_id": backend_id,
        "channel_type": channel_type,
        "group_id": "grp1",
        "group_name": "Fish Dev Group",
        "content": "Agent says hi",
        "push_status": push_status,
        "shard_index": 1,
        "shard_total": 1,
        "retry_count": 0,
        "error_message": None,
        "content_hash": "abc123",
    }


@pytest.fixture
async def db(tmp_path):
    db = MessageDB(tmp_path / "test.db")
    await db.init()
    yield db
    await db.close()


class TestInbound:
    async def test_save_and_get_pending_roundtrip(self, db):
        await db.save_inbound(_make_inbound())
        rows = await db.get_pending_inbound()
        assert len(rows) == 1
        assert rows[0]["id"] == "msg-001"
        assert rows[0]["delivery_status"] == "pending"
        assert rows[0]["content"] == "Hello agent"

    async def test_get_pending_only_returns_pending(self, db):
        await db.save_inbound(_make_inbound("msg-001", "dk-1", delivery_status="pending"))
        await db.save_inbound(_make_inbound("msg-002", "dk-2", delivery_status="delivered"))
        rows = await db.get_pending_inbound()
        assert len(rows) == 1
        assert rows[0]["id"] == "msg-001"

    async def test_update_delivery_status(self, db):
        await db.save_inbound(_make_inbound())
        await db.update_inbound_delivery(
            "msg-001", "delivered", delivered_at="2024-01-01T10:00:01"
        )
        rows = await db.get_pending_inbound()
        assert len(rows) == 0

    async def test_update_delivery_sets_delivered_at(self, db):
        await db.save_inbound(_make_inbound())
        await db.update_inbound_delivery(
            "msg-001", "delivered", delivered_at="2024-01-01T10:00:01"
        )
        # Verify via get_unprocessed_for_backend
        rows = await db.get_unprocessed_for_backend("fish-dev")
        assert rows[0]["delivered_at"] == "2024-01-01T10:00:01"

    async def test_update_delivery_with_error(self, db):
        await db.save_inbound(_make_inbound())
        await db.update_inbound_delivery(
            "msg-001", "failed", error_message="Connection refused"
        )
        # Verify status via pending check — failed is not pending
        pending = await db.get_pending_inbound()
        assert len(pending) == 0

    async def test_update_process_status(self, db):
        await db.save_inbound(
            _make_inbound(delivery_status="delivered", process_status="pending")
        )
        await db.update_inbound_process(
            "msg-001", "processed", processed_at="2024-01-01T10:00:05"
        )
        rows = await db.get_unprocessed_for_backend("fish-dev")
        assert len(rows) == 0

    async def test_dedup_key_unique_prevents_duplicate(self, db):
        import aiosqlite

        await db.save_inbound(_make_inbound())
        with pytest.raises(aiosqlite.IntegrityError):
            await db.save_inbound(_make_inbound("msg-002", dedup_key="dedup-001"))

    async def test_has_dedup_key_true_after_save(self, db):
        await db.save_inbound(_make_inbound())
        assert await db.has_dedup_key("dedup-001") is True

    async def test_has_dedup_key_false_for_unknown(self, db):
        assert await db.has_dedup_key("nonexistent") is False

    async def test_get_unprocessed_for_backend_correct_subset(self, db):
        # delivered + pending → should appear
        await db.save_inbound(
            _make_inbound("msg-001", "dk-1", backend_id="fish-dev",
                          delivery_status="delivered", process_status="pending")
        )
        # delivered + unprocessed_crash → should appear
        await db.save_inbound(
            _make_inbound("msg-002", "dk-2", backend_id="fish-dev",
                          delivery_status="delivered", process_status="unprocessed_crash")
        )
        # delivered + processed → should NOT appear
        await db.save_inbound(
            _make_inbound("msg-003", "dk-3", backend_id="fish-dev",
                          delivery_status="delivered", process_status="processed")
        )
        # pending delivery → should NOT appear
        await db.save_inbound(
            _make_inbound("msg-004", "dk-4", backend_id="fish-dev",
                          delivery_status="pending", process_status="pending")
        )
        # different backend → should NOT appear
        await db.save_inbound(
            _make_inbound("msg-005", "dk-5", backend_id="trade-dev",
                          delivery_status="delivered", process_status="pending")
        )

        rows = await db.get_unprocessed_for_backend("fish-dev")
        ids = {r["id"] for r in rows}
        assert ids == {"msg-001", "msg-002"}


class TestOutbound:
    async def test_save_and_get_pending_roundtrip(self, db):
        await db.save_outbound(_make_outbound())
        rows = await db.get_pending_outbound()
        assert len(rows) == 1
        assert rows[0]["id"] == "out-001"
        assert rows[0]["push_status"] == "pending"
        assert rows[0]["content"] == "Agent says hi"

    async def test_get_pending_only_returns_pending(self, db):
        await db.save_outbound(_make_outbound("out-001", push_status="pending"))
        await db.save_outbound(_make_outbound("out-002", push_status="pushed"))
        rows = await db.get_pending_outbound()
        assert len(rows) == 1
        assert rows[0]["id"] == "out-001"

    async def test_update_outbound_push_changes_status(self, db):
        await db.save_outbound(_make_outbound())
        await db.update_outbound_push("out-001", "pushed", pushed_at="2024-01-01T10:01:05")
        rows = await db.get_pending_outbound()
        assert len(rows) == 0

    async def test_get_failed_outbound_returns_only_failed(self, db):
        await db.save_outbound(_make_outbound("out-001", push_status="pending"))
        await db.save_outbound(_make_outbound("out-002", push_status="failed"))
        await db.save_outbound(_make_outbound("out-003", push_status="pushed"))
        rows = await db.get_failed_outbound()
        assert len(rows) == 1
        assert rows[0]["id"] == "out-002"

    async def test_update_outbound_push_sets_error_message(self, db):
        await db.save_outbound(_make_outbound())
        await db.update_outbound_push("out-001", "failed", error_message="Timeout")
        rows = await db.get_failed_outbound()
        assert rows[0]["error_message"] == "Timeout"


class TestQueryMessages:
    async def test_query_inbound_by_channel_type(self, db):
        await db.save_inbound(_make_inbound("m1", "dk-1", channel_type="feishu"))
        await db.save_inbound(_make_inbound("m2", "dk-2", channel_type="telegram"))
        rows, total = await db.query_messages({"direction": "inbound", "channel_type": "feishu"})
        assert total == 1
        assert rows[0]["id"] == "m1"

    async def test_query_outbound_direction(self, db):
        await db.save_inbound(_make_inbound("m1", "dk-1"))
        await db.save_outbound(_make_outbound("o1"))
        rows, total = await db.query_messages({"direction": "outbound"})
        assert total == 1
        assert rows[0]["id"] == "o1"

    async def test_query_inbound_by_backend_id(self, db):
        await db.save_inbound(_make_inbound("m1", "dk-1", backend_id="fish-dev"))
        await db.save_inbound(_make_inbound("m2", "dk-2", backend_id="trade-dev"))
        rows, total = await db.query_messages({"direction": "inbound", "backend_id": "fish-dev"})
        assert total == 1
        assert rows[0]["backend_id"] == "fish-dev"

    async def test_query_pagination(self, db):
        for i in range(5):
            await db.save_inbound(
                _make_inbound(f"m{i:03d}", dedup_key=f"dk-{i}")
            )
        rows, total = await db.query_messages({"page": 1, "page_size": 2})
        assert total == 5
        assert len(rows) == 2

        rows2, _ = await db.query_messages({"page": 2, "page_size": 2})
        assert len(rows2) == 2
        # No overlap
        ids1 = {r["id"] for r in rows}
        ids2 = {r["id"] for r in rows2}
        assert ids1.isdisjoint(ids2)

    async def test_query_inbound_by_delivery_status(self, db):
        await db.save_inbound(_make_inbound("m1", "dk-1", delivery_status="pending"))
        await db.save_inbound(_make_inbound("m2", "dk-2", delivery_status="delivered"))
        rows, total = await db.query_messages(
            {"direction": "inbound", "delivery_status": "delivered"}
        )
        assert total == 1
        assert rows[0]["id"] == "m2"

    async def test_query_outbound_by_push_status(self, db):
        await db.save_outbound(_make_outbound("o1", push_status="pending"))
        await db.save_outbound(_make_outbound("o2", push_status="failed"))
        rows, total = await db.query_messages(
            {"direction": "outbound", "push_status": "failed"}
        )
        assert total == 1
        assert rows[0]["id"] == "o2"

    async def test_query_empty_returns_zero_total(self, db):
        rows, total = await db.query_messages({"direction": "inbound"})
        assert rows == []
        assert total == 0
