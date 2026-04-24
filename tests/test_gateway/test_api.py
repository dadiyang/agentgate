"""Tests for GatewayAPI HTTP handlers."""

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from unittest.mock import AsyncMock, MagicMock, patch


from agentgate_gateway.api import GatewayAPI, setup_routes


# ------------------------------------------------------------------ #
#  Helper: build a test aiohttp app with mocked dependencies           #
# ------------------------------------------------------------------ #


def create_test_app(test_mode: bool = True):
    """Return (app, gateway, mock_adapter, mock_backend, mock_db, mock_inbound)."""
    config = MagicMock()
    config.test_mode = test_mode
    config.api_token = ""

    db = AsyncMock()
    db.get_pending = AsyncMock(return_value=[])
    db.query_messages = AsyncMock(return_value=([], 0))

    mock_adapter = MagicMock()
    mock_adapter.is_connected = MagicMock(return_value=True)
    mock_adapter._test_disconnected = False
    mock_adapter.test_disconnect = MagicMock()
    mock_adapter.test_reconnect = MagicMock()

    backend = MagicMock()
    backend.status = "healthy"
    backend.url = "http://localhost:8901"
    backend.api_token = "test-token"
    backend.last_check = "2026-03-13T10:00:00Z"
    backend.last_error = None

    inbound = AsyncMock()
    inbound.handle_message = AsyncMock(return_value=None)

    poller = MagicMock()

    gw = GatewayAPI(
        config,
        db,
        MagicMock(),
        {"feishu": mock_adapter},
        {"echo-test": backend},
        inbound,
        poller,
    )
    app = web.Application()
    setup_routes(app, gw)
    return app, gw, mock_adapter, backend, db, inbound


# ------------------------------------------------------------------ #
#  Test cases                                                          #
# ------------------------------------------------------------------ #


class TestHealthEndpoint(AioHTTPTestCase):
    async def get_application(self):
        app, *_ = create_test_app()
        return app

    async def test_health_endpoint(self):
        """GET /api/health returns channels and backends status."""
        resp = await self.client.get("/api/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert "channels" in data
        assert "feishu" in data["channels"]
        assert data["channels"]["feishu"]["status"] == "connected"
        assert "backends" in data
        assert "echo-test" in data["backends"]
        assert data["backends"]["echo-test"]["status"] == "healthy"
        assert data["backends"]["echo-test"]["url"] == "http://localhost:8901"

    async def test_health_includes_pending_counts(self):
        """pending_inbound and pending_outbound are populated from DB."""
        app, gw, *_ = create_test_app()
        # Override to return non-empty lists per direction
        async def _get_pending(direction):
            if direction == "inbound":
                return [{"id": "1"}, {"id": "2"}]
            if direction == "outbound":
                return [{"id": "3"}]
            return []
        gw._db.get_pending = _get_pending

        async with self.client.session.get(
            self.client.make_url("/api/health")
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            # This app instance has the original mocks; use a fresh client for the modified one
        # Use a fresh test client against the modified app
        from aiohttp.test_utils import TestClient, TestServer
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        resp2 = await client.get("/api/health")
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["pending_inbound"] == 2
        assert data2["pending_outbound"] == 1
        await client.close()


class TestHttpInject(AioHTTPTestCase):
    async def get_application(self):
        app, *_ = create_test_app()
        return app

    async def test_http_inject_success(self):
        """POST /api/channel/inject with valid backend → 200, inbound.handle_message called."""
        app, gw, _, _, _, inbound = create_test_app()
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/channel/inject",
                json={
                    "backend_id": "echo-test",
                    "text": "hello world",
                    "sender_id": "ci-user",
                    "sender_name": "CI",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["backend_id"] == "echo-test"
            assert "message_id" in data
            inbound.handle_message.assert_called_once()
            call_kwargs = inbound.handle_message.call_args
            assert call_kwargs.kwargs.get("channel_type") == "http" or call_kwargs.args[0] == "http"

    async def test_http_inject_missing_backend_id(self):
        """No backend_id → 400."""
        resp = await self.client.post(
            "/api/channel/inject",
            json={"text": "hello"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False
        assert data["error"] == "bad_request"

    async def test_http_inject_unknown_backend(self):
        """Non-existent backend → 404."""
        resp = await self.client.post(
            "/api/channel/inject",
            json={"backend_id": "no-such-backend", "text": "hello"},
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["ok"] is False
        assert data["error"] == "backend_not_found"

    async def test_http_inject_unhealthy_backend(self):
        """Unhealthy backend → 503."""
        app, gw, _, backend, *_ = create_test_app()
        backend.status = "unhealthy"
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/channel/inject",
                json={"backend_id": "echo-test", "text": "hello"},
            )
            assert resp.status == 503
            data = await resp.json()
            assert data["ok"] is False
            assert data["error"] == "backend_unhealthy"


class TestAdminEndpoints(AioHTTPTestCase):
    async def get_application(self):
        app, *_ = create_test_app(test_mode=True)
        return app

    async def test_admin_disconnect(self):
        """POST /api/admin/adapter/feishu/disconnect → 200, adapter.test_disconnect called."""
        app, gw, mock_adapter, *_ = create_test_app(test_mode=True)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/admin/adapter/feishu/disconnect",
                json={},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["adapter"] == "feishu"
            assert data["status"] == "disconnected"
            mock_adapter.test_disconnect.assert_called_once_with(0)

    async def test_admin_disconnect_with_duration(self):
        """duration_seconds=30 → passed to test_disconnect."""
        app, gw, mock_adapter, *_ = create_test_app(test_mode=True)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/admin/adapter/feishu/disconnect",
                json={"duration_seconds": 30},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["auto_reconnect_after"] == 30
            mock_adapter.test_disconnect.assert_called_once_with(30)

    async def test_admin_reconnect(self):
        """POST /api/admin/adapter/feishu/reconnect → 200, adapter.test_reconnect called."""
        app, gw, mock_adapter, *_ = create_test_app(test_mode=True)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/admin/adapter/feishu/reconnect", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["adapter"] == "feishu"
            assert data["status"] == "connected"
            mock_adapter.test_reconnect.assert_called_once()

    async def test_admin_adapter_not_found(self):
        """Unknown adapter → 404."""
        resp = await self.client.post(
            "/api/admin/adapter/telegram/disconnect",
            json={},
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["ok"] is False
        assert data["error"] == "adapter_not_found"

    async def test_admin_test_mode_status(self):
        """GET /api/admin/test-mode → shows all adapters."""
        resp = await self.client.get("/api/admin/test-mode")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["test_mode"] is True
        assert "adapters" in data
        assert "feishu" in data["adapters"]

    async def test_admin_routes_not_registered_when_test_mode_false(self):
        """test_mode=False → admin routes return 404."""
        app, *_ = create_test_app(test_mode=False)
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/admin/adapter/feishu/disconnect",
                json={},
            )
            assert resp.status == 404

            resp2 = await client.get("/api/admin/test-mode")
            assert resp2.status == 404


class TestMessagesQuery(AioHTTPTestCase):
    async def get_application(self):
        app, *_ = create_test_app()
        return app

    async def test_messages_query(self):
        """POST /api/messages/query → delegates to db.query_messages."""
        app, gw, *_ = create_test_app()
        sample_messages = [{"id": "m1", "content": "hello"}]
        gw._db.query_messages = AsyncMock(return_value=(sample_messages, 1))

        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/messages/query",
                json={
                    "direction": "inbound",
                    "page": 1,
                    "page_size": 50,
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["total"] == 1
            assert data["page"] == 1
            assert data["page_size"] == 50
            assert len(data["messages"]) == 1
            assert data["messages"][0]["id"] == "m1"
            gw._db.query_messages.assert_called_once()
