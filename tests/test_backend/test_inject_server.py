"""Tests for extended inject_server — idempotency, confirm_processed, unprocessed."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import AioHTTPTestCase

from agentgate_backend.delivery_tracker import DeliveryTracker
from agentgate_backend.inject_server import create_app
from agentgate_backend.message_store import MessageStore


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def _make_window(window_id: str = "@0", window_name: str = "test-window"):
    w = MagicMock()
    w.window_id = window_id
    w.window_name = window_name
    w.pane_current_command = "claude"
    return w


def _build_app(
    *,
    window_name: str = "test-window",
    window_id: str = "@0",
    send_success: bool = True,
    message_store: MessageStore | None = None,
):
    """Return an aiohttp Application with mocked dependencies."""
    window = _make_window(window_id=window_id, window_name=window_name)

    tmux = MagicMock()
    tmux.list_windows = AsyncMock(return_value=[window])
    tmux.find_window_by_name = AsyncMock(return_value=window)
    tmux.find_window_by_id = AsyncMock(return_value=window)
    tmux.create_window = AsyncMock(return_value=(True, "created", window_name, window_id))

    tracker = DeliveryTracker()

    session_mgr = MagicMock()
    session_mgr.window_states = {}
    msg = "Sent to test-window" if send_success else "Failed"
    session_mgr.send_to_window = AsyncMock(return_value=(send_success, msg))
    session_mgr.get_recent_messages = AsyncMock(return_value=([], 0))
    session_mgr.resolve_session_for_window = AsyncMock(return_value=None)

    ms = message_store if message_store is not None else MessageStore()

    app = create_app(
        tracker=tracker,
        session_manager=session_mgr,
        tmux_manager=tmux,
        api_token="",
        message_store=ms,
    )
    return app, tmux, tracker, session_mgr, ms


# ------------------------------------------------------------------ #
#  Test cases                                                          #
# ------------------------------------------------------------------ #


class TestHealthEndpoint(AioHTTPTestCase):
    async def get_application(self):
        app, *_ = _build_app()
        return app

    async def test_health_endpoint(self):
        """GET /api/health returns status ok with windows list."""
        resp = await self.client.get("/api/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "windows" in data
        assert "uptime_seconds" in data


class TestInjectIdempotency(AioHTTPTestCase):
    async def get_application(self):
        self._store = MessageStore()
        app, self._tmux, self._tracker, self._session_mgr, _ = _build_app(
            message_store=self._store,
        )
        return app

    async def test_inject_idempotent_first_call_injects(self):
        """First inject with message_id calls send_to_window."""
        resp = await self.client.post(
            "/api/inject",
            json={"window_name": "test-window", "text": "hello", "message_id": "msg-001"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data.get("duplicate") is not True
        self._session_mgr.send_to_window.assert_called_once()

    async def test_inject_idempotent_second_call_skips_send_keys(self):
        """Second inject with same message_id returns ok=True but skips send_to_window."""
        payload = {"window_name": "test-window", "text": "hello", "message_id": "msg-dup"}

        # First call
        r1 = await self.client.post("/api/inject", json=payload)
        assert r1.status == 200
        d1 = await r1.json()
        assert d1["ok"] is True
        assert d1.get("duplicate") is not True

        # Second call — same message_id
        r2 = await self.client.post("/api/inject", json=payload)
        assert r2.status == 200
        d2 = await r2.json()
        assert d2["ok"] is True
        assert d2.get("duplicate") is True

        # send_to_window called exactly once (not twice)
        assert self._session_mgr.send_to_window.call_count == 1


class TestConfirmProcessed(AioHTTPTestCase):
    async def get_application(self):
        self._store = MessageStore()
        app, *_ = _build_app(message_store=self._store)
        return app

    async def test_confirm_processed_removes_from_unprocessed(self):
        """Inject a message, then confirm it — unprocessed should be empty."""
        # Inject with a message_id
        inj = await self.client.post(
            "/api/inject",
            json={
                "window_name": "test-window",
                "text": "do something",
                "message_id": "track-001",
            },
        )
        assert inj.status == 200

        # Verify it's in unprocessed
        unproc = await self.client.get("/api/unprocessed")
        assert unproc.status == 200
        unproc_data = await unproc.json()
        ids = [item["message_id"] for item in unproc_data["unprocessed"]]
        assert "track-001" in ids

        # Confirm
        conf = await self.client.post(
            "/api/confirm_processed",
            json={"message_ids": ["track-001"]},
        )
        assert conf.status == 200
        conf_data = await conf.json()
        assert conf_data["ok"] is True
        assert conf_data["confirmed"] == 1
        assert "track-001" in conf_data["message_ids"]

        # Now unprocessed should be empty
        unproc2 = await self.client.get("/api/unprocessed")
        data2 = await unproc2.json()
        ids2 = [item["message_id"] for item in data2["unprocessed"]]
        assert "track-001" not in ids2

    async def test_confirm_nonexistent_message_id(self):
        """Confirming an unknown message_id returns ok=True with confirmed=0."""
        resp = await self.client.post(
            "/api/confirm_processed",
            json={"message_ids": ["ghost-id"]},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["confirmed"] == 0
        assert "ghost-id" in data["message_ids"]

    async def test_confirm_missing_message_id_field(self):
        """Confirm without message_id field returns 400."""
        resp = await self.client.post("/api/confirm_processed", json={})
        assert resp.status == 400


class TestUnprocessed(AioHTTPTestCase):
    async def get_application(self):
        self._store = MessageStore()
        app, *_ = _build_app(message_store=self._store)
        return app

    async def test_unprocessed_returns_pending_messages(self):
        """Inject without confirming — unprocessed returns those messages."""
        for i in range(3):
            await self.client.post(
                "/api/inject",
                json={
                    "window_name": "test-window",
                    "text": f"task {i}",
                    "message_id": f"pending-{i}",
                },
            )

        resp = await self.client.get("/api/unprocessed")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["count"] == 3
        ids = {item["message_id"] for item in data["unprocessed"]}
        assert ids == {"pending-0", "pending-1", "pending-2"}

    async def test_unprocessed_empty_initially(self):
        """No injections — unprocessed returns empty list."""
        resp = await self.client.get("/api/unprocessed")
        assert resp.status == 200
        data = await resp.json()
        assert data["count"] == 0
        assert data["unprocessed"] == []

    async def test_inject_without_message_id_not_tracked(self):
        """Inject without message_id doesn't appear in unprocessed (no tracking key)."""
        await self.client.post(
            "/api/inject",
            json={"window_name": "test-window", "text": "anonymous"},
        )
        resp = await self.client.get("/api/unprocessed")
        data = await resp.json()
        assert data["count"] == 0
