"""Integration smoke test: echo_backend + gateway + HTTP channel.

Verifies the full message lifecycle:
  HTTP inject → Gateway persist → route to echo_backend → echo reply → Gateway poll → output available
"""

import asyncio
import pytest
import httpx
from aiohttp import web

from echo_backend.main import make_app as echo_make_app
from agentgate_gateway.config import GatewayConfig, BackendConfig as GwBackendConfig
from agentgate_gateway.db import MessageDB
from agentgate_gateway.router import Router
from agentgate_gateway.health_prober import BackendState
from agentgate_gateway.inbound_handler import InboundHandler
from agentgate_gateway.output_poller import OutputPoller
from agentgate_gateway.recovery import RecoveryManager
from agentgate_gateway.api import GatewayAPI, setup_routes


@pytest.fixture
async def echo_server():
    """Start echo backend on a random port."""
    app = echo_make_app(token="test-token")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Get actual port
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", "test-token"
    await runner.cleanup()


@pytest.fixture
async def gateway_app(echo_server, tmp_path):
    """Start gateway connected to echo backend."""
    echo_url, echo_token = echo_server

    # Build config
    config = GatewayConfig(
        port=0,
        db_path=tmp_path / "messages.db",
        test_mode=True,
        backends={"echo-test": GwBackendConfig(url=echo_url, api_token=echo_token, agent_type="echo")},
        routes=[],  # HTTP channel doesn't use routes
    )

    # Init DB
    db = MessageDB(config.db_path)
    await db.init()

    # Router (empty — HTTP channel bypasses routing)
    router = Router(config.routes)

    # Backend states
    backend_states = {"echo-test": BackendState(url=echo_url, api_token=echo_token)}
    backend_states["echo-test"].status = "healthy"

    # Adapters (none for HTTP-only test)
    adapters = {}

    # Inbound handler
    inbound = InboundHandler(db, router, backend_states, adapters)

    # Output poller (not started — we poll manually in tests)
    poller = OutputPoller(db, router, backend_states, adapters, poll_interval=999)

    # Recovery
    recovery = RecoveryManager(db, inbound.reinject_message, poller.repush_message)

    # Gateway API
    gw_api = GatewayAPI(config, db, router, adapters, backend_states, inbound, poller)
    app = web.Application()
    setup_routes(app, gw_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}", db, backend_states

    await runner.cleanup()
    await inbound.close()
    await poller.close()
    await db.close()


class TestIntegrationSmoke:
    """Full-chain integration: HTTP inject → echo → output."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, gateway_app):
        gw_url, db, _ = gateway_app
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{gw_url}/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "echo-test" in data["backends"]
            assert data["backends"]["echo-test"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_inject_and_read_output(self, gateway_app):
        gw_url, db, _ = gateway_app
        async with httpx.AsyncClient() as client:
            # Inject message via HTTP channel
            resp = await client.post(
                f"{gw_url}/api/channel/inject",
                json={
                    "backend_id": "echo-test",
                    "text": "hello integration test",
                    "sender_name": "tester",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"]

            # Wait for echo backend to process
            await asyncio.sleep(0.3)

            # Read output via HTTP channel
            resp = await client.get(f"{gw_url}/api/channel/output/echo-test?since=0")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"]
            # Check echo reply exists
            messages = data.get("messages", [])
            assert len(messages) >= 1
            assert any("Echo: hello integration test" in m.get("text", "") for m in messages)

    @pytest.mark.asyncio
    async def test_inject_unknown_backend(self, gateway_app):
        gw_url, _, _ = gateway_app
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gw_url}/api/channel/inject",
                json={"backend_id": "nonexistent", "text": "hi"},
            )
            assert resp.status_code == 404
            data = resp.json()
            assert data["error"] == "backend_not_found"

    @pytest.mark.asyncio
    async def test_inject_missing_backend_id(self, gateway_app):
        gw_url, _, _ = gateway_app
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gw_url}/api/channel/inject",
                json={"text": "hi"},
            )
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_thinking_trigger_filtered(self, gateway_app):
        """Echo backend returns thinking + text; output should contain both (gateway filters on push, not on read)."""
        gw_url, _, _ = gateway_app
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gw_url}/api/channel/inject",
                json={
                    "backend_id": "echo-test",
                    "text": "[test-thinking] analyze code",
                    "sender_name": "tester",
                },
            )
            assert resp.status_code == 200

            await asyncio.sleep(0.3)

            resp = await client.get(f"{gw_url}/api/channel/output/echo-test?since=0")
            data = resp.json()
            messages = data.get("messages", [])
            content_types = [m.get("content_type", "") for m in messages]
            # Echo backend produces both thinking and text
            assert "thinking" in content_types
            assert "text" in content_types

    @pytest.mark.asyncio
    async def test_admin_disconnect_reconnect(self, gateway_app):
        """Admin API test mode works (even though no IM adapters in this test)."""
        gw_url, _, _ = gateway_app
        async with httpx.AsyncClient() as client:
            # Test mode status
            resp = await client.get(f"{gw_url}/api/admin/test-mode")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"]
            assert data["test_mode"]

    @pytest.mark.asyncio
    async def test_messages_query(self, gateway_app):
        """Message query API returns persisted messages."""
        gw_url, db, _ = gateway_app
        async with httpx.AsyncClient() as client:
            # Inject a message first
            await client.post(
                f"{gw_url}/api/channel/inject",
                json={"backend_id": "echo-test", "text": "query test", "sender_name": "tester"},
            )
            await asyncio.sleep(0.2)

            # Query messages
            resp = await client.post(
                f"{gw_url}/api/messages/query",
                json={"direction": "inbound", "page": 1, "page_size": 50},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"]
            assert data["total"] >= 1
            # Find our message
            found = any("query test" in m.get("content", "") for m in data["messages"])
            assert found

    @pytest.mark.asyncio
    async def test_unhealthy_backend_rejects_inject(self, gateway_app):
        """Unhealthy backend returns 503."""
        gw_url, _, backend_states = gateway_app
        # Mark backend unhealthy
        backend_states["echo-test"].status = "unhealthy"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{gw_url}/api/channel/inject",
                json={"backend_id": "echo-test", "text": "should fail"},
            )
            assert resp.status_code == 503
        # Restore
        backend_states["echo-test"].status = "healthy"
