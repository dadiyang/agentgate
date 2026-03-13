"""Gateway HTTP API and Admin API handlers."""

import logging
import time
import uuid

import httpx
from aiohttp import web

logger = logging.getLogger(__name__)


def _check_gateway_auth(request: web.Request, api_token: str) -> web.Response | None:
    """Return error response if Gateway API auth fails, None if OK."""
    if not api_token:
        return None  # No token configured = no auth required
    auth = request.headers.get("Authorization", "")
    if not auth:
        return _json({"ok": False, "error": "unauthorized", "msg": "Missing Authorization header"}, 401)
    if auth != f"Bearer {api_token}":
        return _json({"ok": False, "error": "forbidden", "msg": "Invalid token"}, 403)
    return None


class GatewayAPI:
    def __init__(self, config, db, router, adapters, backends, inbound_handler, output_poller):
        """
        config:          GatewayConfig
        db:              MessageDB
        router:          Router
        adapters:        dict[str, ChannelAdapter]
        backends:        dict[str, BackendState]
        inbound_handler: InboundHandler
        output_poller:   OutputPoller
        """
        self.config = config
        self._db = db
        self._router = router
        self._adapters = adapters
        self._backends = backends
        self._inbound = inbound_handler
        self._poller = output_poller
        self._start_time = time.time()

    # ------------------------------------------------------------------ #
    #  Public endpoints                                                     #
    # ------------------------------------------------------------------ #

    async def handle_http_inject(self, request: web.Request) -> web.Response:
        """POST /api/channel/inject — HTTP channel message injection."""
        auth_err = _check_gateway_auth(request, self.config.api_token)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
        except Exception:
            return _json({"ok": False, "error": "bad_request", "msg": "Invalid JSON body"}, 400)

        backend_id = body.get("backend_id", "").strip()
        if not backend_id:
            return _json({"ok": False, "error": "bad_request", "msg": "backend_id required"}, 400)

        if backend_id not in self._backends:
            return _json(
                {"ok": False, "error": "backend_not_found", "msg": f"Backend '{backend_id}' not configured"},
                404,
            )

        backend = self._backends[backend_id]
        status = getattr(backend, "status", None)
        if status == "unhealthy":
            return _json(
                {"ok": False, "error": "backend_unhealthy", "msg": f"Backend '{backend_id}' is unhealthy"},
                503,
            )

        text = body.get("text", "")
        sender_id = body.get("sender_id", "api-user")
        sender_name = body.get("sender_name", "HTTP API")
        dedup_key = f"http:{str(uuid.uuid4())}"
        message_id = str(uuid.uuid4())

        # Go through full pipeline (persist → inject to backend)
        # HTTP channel bypasses routing — backend_id is explicit
        # M-6: Pass message_id so DB and API response use the same ID
        await self._inbound.handle_message(
            channel_type="http",
            bot_id="",
            group_id=backend_id,
            sender_id=sender_id,
            sender_name=sender_name,
            group_name="",
            text=text,
            dedup_key=dedup_key,
            target_backend_id=backend_id,
            message_id=message_id,
        )

        return _json({"ok": True, "message_id": message_id, "backend_id": backend_id}, 200)

    async def handle_http_output(self, request: web.Request) -> web.Response:
        """GET /api/channel/output/{backend_id}?since={offset} — Proxy to backend /api/output."""
        auth_err = _check_gateway_auth(request, self.config.api_token)
        if auth_err:
            return auth_err
        backend_id = request.match_info["backend_id"]

        if backend_id not in self._backends:
            return _json(
                {"ok": False, "error": "backend_not_found", "msg": f"Backend '{backend_id}' not configured"},
                404,
            )

        backend = self._backends[backend_id]
        url = getattr(backend, "url", "")
        token = getattr(backend, "api_token", "")
        since = request.rel_url.query.get("since", "0")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{url}/api/output/default?since={since}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            data = resp.json()
            return _json({"ok": True, "backend_id": backend_id, **data}, resp.status_code)
        except Exception as e:
            logger.error("handle_http_output proxy error: %s", e, exc_info=True)
            return _json({"ok": False, "error": "proxy_error", "msg": str(e)}, 502)

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /api/health — Gateway overall health (AC-32/33)."""
        uptime = time.time() - self._start_time

        channels = {}
        for name, adapter in self._adapters.items():
            connected = adapter.is_connected() if callable(adapter.is_connected) else False
            channels[name] = {"status": "connected" if connected else "disconnected"}

        backends = {}
        for bid, state in self._backends.items():
            backends[bid] = {
                "status": getattr(state, "status", "unknown"),
                "url": getattr(state, "url", ""),
                "last_check": getattr(state, "last_check", None),
                "last_error": getattr(state, "last_error", None),
            }

        try:
            pending_inbound = len(await self._db.get_pending_inbound())
        except Exception:
            pending_inbound = 0

        try:
            pending_outbound = len(await self._db.get_pending_outbound())
        except Exception:
            pending_outbound = 0

        return _json(
            {
                "status": "ok",
                "uptime_seconds": round(uptime, 1),
                "channels": channels,
                "backends": backends,
                "pending_inbound": pending_inbound,
                "pending_outbound": pending_outbound,
            },
            200,
        )

    async def handle_messages_query(self, request: web.Request) -> web.Response:
        """POST /api/messages/query — Message query (F13 AC-34~39)."""
        auth_err = _check_gateway_auth(request, self.config.api_token)
        if auth_err:
            return auth_err
        try:
            filters = await request.json()
        except Exception:
            return _json({"ok": False, "error": "bad_request", "msg": "Invalid JSON body"}, 400)

        try:
            messages, total = await self._db.query_messages(filters)
        except Exception as e:
            logger.error("handle_messages_query db error: %s", e, exc_info=True)
            return _json({"ok": False, "error": "db_error", "msg": str(e)}, 500)

        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))

        return _json(
            {
                "ok": True,
                "total": total,
                "page": page,
                "page_size": page_size,
                "messages": messages,
            },
            200,
        )

    # ------------------------------------------------------------------ #
    #  Admin endpoints (only registered when test_mode=True)              #
    # ------------------------------------------------------------------ #

    async def _require_test_mode(self) -> web.Response | None:
        """Return 403 response if test mode is disabled, else None."""
        if not self.config.test_mode:
            return _json(
                {"ok": False, "error": "test_mode_disabled", "msg": "Admin API requires --test-mode"},
                403,
            )
        return None

    async def handle_admin_disconnect(self, request: web.Request) -> web.Response:
        """POST /api/admin/adapter/{name}/disconnect — Simulate channel disconnect."""
        guard = await self._require_test_mode()
        if guard:
            return guard

        name = request.match_info["name"]
        if name not in self._adapters:
            return _json(
                {"ok": False, "error": "adapter_not_found", "msg": f"Adapter '{name}' not configured"},
                404,
            )

        try:
            body = await request.json()
        except Exception:
            body = {}

        duration = int(body.get("duration_seconds", 0))
        adapter = self._adapters[name]
        adapter.test_disconnect(duration)

        return _json(
            {
                "ok": True,
                "adapter": name,
                "status": "disconnected",
                "auto_reconnect_after": duration,
            },
            200,
        )

    async def handle_admin_reconnect(self, request: web.Request) -> web.Response:
        """POST /api/admin/adapter/{name}/reconnect — Restore channel connection."""
        guard = await self._require_test_mode()
        if guard:
            return guard

        name = request.match_info["name"]
        if name not in self._adapters:
            return _json(
                {"ok": False, "error": "adapter_not_found", "msg": f"Adapter '{name}' not configured"},
                404,
            )

        adapter = self._adapters[name]
        adapter.test_reconnect()

        return _json({"ok": True, "adapter": name, "status": "connected"}, 200)

    async def handle_admin_test_mode(self, request: web.Request) -> web.Response:
        """GET /api/admin/test-mode — Query test mode status."""
        guard = await self._require_test_mode()
        if guard:
            return guard

        adapters_info = {}
        for name, adapter in self._adapters.items():
            connected = adapter.is_connected() if callable(adapter.is_connected) else False
            adapters_info[name] = {
                "connected": connected,
                "test_disconnected": getattr(adapter, "_test_disconnected", False),
            }

        return _json(
            {
                "ok": True,
                "test_mode": True,
                "adapters": adapters_info,
            },
            200,
        )


# ------------------------------------------------------------------ #
#  Route registration                                                  #
# ------------------------------------------------------------------ #


def setup_routes(app: web.Application, gateway: GatewayAPI) -> None:
    """Register all routes on the aiohttp Application."""
    app.router.add_post("/api/channel/inject", gateway.handle_http_inject)
    app.router.add_get("/api/channel/output/{backend_id}", gateway.handle_http_output)
    app.router.add_get("/api/health", gateway.handle_health)
    app.router.add_post("/api/messages/query", gateway.handle_messages_query)

    if gateway.config.test_mode:
        app.router.add_post("/api/admin/adapter/{name}/disconnect", gateway.handle_admin_disconnect)
        app.router.add_post("/api/admin/adapter/{name}/reconnect", gateway.handle_admin_reconnect)
        app.router.add_get("/api/admin/test-mode", gateway.handle_admin_test_mode)


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def _json(data: dict, status: int = 200) -> web.Response:
    """Return a JSON response."""
    return web.json_response(data, status=status)
