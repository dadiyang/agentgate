"""HTTP API server for message injection, output reading, and window management.

Extended from ccbot inject_server with:
  - Message ID idempotency (MessageStore) on POST /api/inject
  - POST /api/confirm_processed — gateway confirms message was processed by agent
  - GET  /api/unprocessed — gateway queries injected-but-unconfirmed messages

All existing ccbot routes are preserved:
  POST /api/inject  -- inject text into a tmux window
  GET  /api/health  -- window status and pending deliveries
  GET  /api/delivery/{id} -- single delivery status
  GET  /api/output/{window_name} -- read session output (incremental via byte offset)
  POST /api/window  -- create a new tmux window with Claude Code

All endpoints require Bearer token authentication when api_token is configured.
Runs inside asyncio event loop via aiohttp web.AppRunner.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .message_store import MessageStore

if TYPE_CHECKING:
    from .delivery_tracker import DeliveryTracker
    from .self_monitor import SelfMonitor
    from .session import SessionManager
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)

_startup_time: float = 0.0


def _check_auth(request: web.Request, api_token: str) -> web.Response | None:
    """Return error response if auth fails, None if OK."""
    if not api_token:
        return None  # No token configured = no auth required
    auth = request.headers.get("Authorization", "")
    if not auth:
        return web.json_response(
            {"ok": False, "error": "unauthorized", "msg": "Missing Authorization header"},
            status=401,
        )
    if auth != f"Bearer {api_token}":
        return web.json_response(
            {"ok": False, "error": "forbidden", "msg": "Invalid token"},
            status=403,
        )
    return None


def create_app(
    *,
    tracker: DeliveryTracker,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    api_token: str = "",
    self_monitor: SelfMonitor | None = None,
    message_store: MessageStore | None = None,
) -> web.Application:
    """Create aiohttp app with all routes."""
    if message_store is None:
        message_store = MessageStore()

    # Track injected-but-unconfirmed messages: message_id -> info dict
    _unprocessed: dict[str, dict] = {}

    async def health_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err

        windows = await tmux_manager.list_windows()
        window_list = []
        for w in windows:
            ws = session_manager.window_states.get(w.window_id)
            sid = ws.session_id if ws else ""
            window_list.append({
                "window_id": w.window_id,
                "window_name": w.window_name,
                "pane_command": getattr(w, "pane_current_command", "unknown"),
                "session_id": sid,
                "pending_deliveries": tracker.pending_count(w.window_id),
            })

        watchdog_enabled = self_monitor is not None
        window_health: dict = {}
        if self_monitor is not None:
            window_health = self_monitor.window_statuses

        return web.json_response({
            "status": "ok",
            "windows": window_list,
            "uptime_seconds": int(time.monotonic() - _startup_time),
            "watchdog_enabled": watchdog_enabled,
            "window_health": window_health,
        })

    async def inject_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            logger.warning("inject_handler: invalid JSON body from %s", request.remote)
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "Invalid JSON"},
                status=400,
            )

        window_name = body.get("window_name", "")
        text = body.get("text", "")
        track = body.get("track_delivery", True)
        message_id = body.get("message_id", "")

        if not window_name or not text:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "window_name and text required"},
                status=400,
            )

        # Idempotency check: if we've already injected this message_id, skip send_keys
        if message_id and message_store.has(message_id):
            logger.info(
                "HTTP inject: duplicate message_id=%s, skipping send_keys", message_id
            )
            return web.json_response({
                "ok": True,
                "duplicate": True,
                "message_id": message_id,
                "msg": "Duplicate message_id — already injected",
            })

        # Find window by name
        window = await tmux_manager.find_window_by_name(window_name)
        if not window:
            return web.json_response(
                {"ok": False, "error": "window_not_found",
                 "msg": f"Window '{window_name}' not found"},
                status=404,
            )

        # Inject via session_manager
        success, msg = await session_manager.send_to_window(window.window_id, text)
        if not success:
            return web.json_response(
                {"ok": False, "error": "inject_failed", "msg": msg},
                status=500,
            )

        # Record message_id in idempotency store
        if message_id:
            message_store.add(message_id)
            # Track as unprocessed until gateway confirms
            _unprocessed[message_id] = {
                "message_id": message_id,
                "window_name": window_name,
                "window_id": window.window_id,
                "text_hint": text[:80],
                "injected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

        delivery_id = None
        if track:
            delivery_id = tracker.register(window.window_id, text)

        logger.info(
            "HTTP inject: window=%s delivery=%s message_id=%s len=%d",
            window_name, delivery_id or "untracked", message_id or "none", len(text),
        )

        return web.json_response({
            "ok": True,
            "delivery_id": delivery_id,
            "window_id": window.window_id,
            "message_id": message_id or None,
            "msg": msg,
        })

    async def delivery_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err

        delivery_id = request.match_info["delivery_id"]
        info = tracker.get_status(delivery_id)
        if info is None:
            return web.json_response(
                {"error": "not_found", "msg": f"Delivery '{delivery_id}' not found"},
                status=404,
            )
        return web.json_response(info)

    async def output_handler(request: web.Request) -> web.Response:
        """Read session output for a window (incremental via byte offset).

        GET /api/output/{window_name}?since=0
        Returns parsed messages from the session JSONL file.
        """
        err = _check_auth(request, api_token)
        if err:
            return err

        window_name = request.match_info["window_name"]
        since = int(request.query.get("since", "0"))

        window = await tmux_manager.find_window_by_name(window_name)
        if not window:
            return web.json_response(
                {"ok": False, "error": "window_not_found",
                 "msg": f"Window '{window_name}' not found"},
                status=404,
            )

        # Ensure session_map is loaded so we can resolve window → JSONL file
        await session_manager.load_session_map()

        messages, count = await session_manager.get_recent_messages(
            window.window_id, start_byte=since,
        )

        session = await session_manager.resolve_session_for_window(window.window_id)
        next_offset = since
        if session and session.file_path:
            try:
                next_offset = Path(session.file_path).stat().st_size
            except OSError:
                pass

        return web.json_response({
            "ok": True,
            "window_name": window_name,
            "window_id": window.window_id,
            "messages": messages,
            "count": count,
            "since": since,
            "next_offset": next_offset,
        })

    async def window_create_handler(request: web.Request) -> web.Response:
        """Create a new tmux window and start Claude Code.

        POST /api/window
        {"work_dir": "/path/to/project", "window_name": "my-project", "start_claude": true}
        """
        err = _check_auth(request, api_token)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "Invalid JSON"},
                status=400,
            )

        work_dir = body.get("work_dir", "")
        window_name = body.get("window_name", "")
        start_claude = body.get("start_claude", True)

        if not work_dir:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "work_dir required"},
                status=400,
            )

        success, msg, final_name, window_id = await tmux_manager.create_window(
            work_dir=work_dir,
            window_name=window_name or None,
            start_claude=start_claude,
        )

        if not success:
            return web.json_response(
                {"ok": False, "error": "create_failed", "msg": msg},
                status=500,
            )

        logger.info(
            "HTTP window create: name=%s id=%s dir=%s",
            final_name, window_id, work_dir,
        )

        return web.json_response({
            "ok": True,
            "window_name": final_name,
            "window_id": window_id,
            "work_dir": work_dir,
            "msg": msg,
        })

    async def confirm_processed_handler(request: web.Request) -> web.Response:
        """Gateway calls this to confirm messages were processed by the agent.

        POST /api/confirm_processed
        {"message_ids": ["abc123", "def456"]}

        Also accepts legacy single-message format: {"message_id": "abc123"}

        Once confirmed, message_ids are removed from the unprocessed tracking dict.
        Note: the idempotency store (MessageStore) retains them for TTL to prevent
        re-injection of the same message.
        """
        err = _check_auth(request, api_token)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "Invalid JSON"},
                status=400,
            )

        # Accept both list and single formats
        message_ids = body.get("message_ids", [])
        if not message_ids:
            single = body.get("message_id", "")
            if single:
                message_ids = [single]

        if not message_ids:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "message_ids (list) or message_id (string) required"},
                status=400,
            )

        confirmed = 0
        for mid in message_ids:
            if mid in _unprocessed:
                del _unprocessed[mid]
                confirmed += 1

        logger.info(
            "confirm_processed: message_ids=%s confirmed=%d",
            message_ids, confirmed,
        )

        return web.json_response({
            "ok": True,
            "confirmed": confirmed,
            "message_ids": message_ids,
        })

    async def unprocessed_handler(request: web.Request) -> web.Response:
        """Return messages that were injected but not yet confirmed as processed.

        GET /api/unprocessed

        The gateway uses this to detect messages that may have been lost
        (e.g. backend restarted before agent responded, network timeout, etc.).
        """
        err = _check_auth(request, api_token)
        if err:
            return err

        items = list(_unprocessed.values())
        return web.json_response({
            "ok": True,
            "count": len(items),
            "unprocessed": items,
        })

    app = web.Application()
    app.router.add_get("/api/health", health_handler)
    app.router.add_post("/api/inject", inject_handler)
    app.router.add_get("/api/delivery/{delivery_id}", delivery_handler)
    app.router.add_get("/api/output/{window_name}", output_handler)
    app.router.add_post("/api/window", window_create_handler)
    app.router.add_post("/api/confirm_processed", confirm_processed_handler)
    app.router.add_get("/api/unprocessed", unprocessed_handler)

    return app


async def start_server(
    *,
    tracker: DeliveryTracker,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    api_token: str = "",
    port: int = 8901,
    self_monitor: SelfMonitor | None = None,
    message_store: MessageStore | None = None,
) -> web.AppRunner:
    """Start the HTTP server (Claude Code mode). Returns runner for lifecycle management."""
    global _startup_time
    _startup_time = time.monotonic()

    app = create_app(
        tracker=tracker,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        api_token=api_token,
        self_monitor=self_monitor,
        message_store=message_store,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("HTTP inject server started on 127.0.0.1:%d", port)
    return runner


# ------------------------------------------------------------------
#  OpenCode mode — lightweight server using OpenCodeDriver
# ------------------------------------------------------------------


def create_opencode_app(
    *,
    driver,  # OpenCodeDriver
    api_token: str = "",
) -> web.Application:
    """Create aiohttp app for OpenCode backend mode."""

    async def health_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err
        health = driver.get_health()
        return web.json_response({
            "status": "ok",
            "windows": [{
                "window_id": "opencode",
                "window_name": "opencode",
                "pane_command": "opencode",
                "session_id": health.get("session_id", ""),
                "pending_deliveries": health.get("queue_size", 0),
            }],
            "uptime_seconds": int(time.monotonic() - _startup_time),
            "watchdog_enabled": False,
            "window_health": {},
            "opencode": health,
        })

    async def inject_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "Invalid JSON"},
                status=400,
            )

        text = body.get("text", "")
        message_id = body.get("message_id", "")
        if not text:
            return web.json_response(
                {"ok": False, "error": "bad_request", "msg": "text required"},
                status=400,
            )

        result = await driver.inject(text, message_id=message_id or None)

        logger.info(
            "HTTP inject (opencode): message_id=%s len=%d queued=%s",
            message_id or "none", len(text), result.get("queued", False),
        )

        return web.json_response({
            "ok": True,
            "delivery_id": message_id or None,
            "window_id": "opencode",
            "message_id": message_id or None,
            "msg": "Injected" if not result.get("queued") else "Queued",
        })

    async def output_handler(request: web.Request) -> web.Response:
        err = _check_auth(request, api_token)
        if err:
            return err
        # window_name is ignored — opencode mode has a single virtual window
        since = int(request.query.get("since", "0"))
        result = driver.get_output(since)
        result["window_name"] = "opencode"
        result["window_id"] = "opencode"
        return web.json_response(result)

    app = web.Application()
    app.router.add_get("/api/health", health_handler)
    app.router.add_post("/api/inject", inject_handler)
    app.router.add_get("/api/output/{window_name}", output_handler)

    return app


async def start_server_opencode(
    *,
    driver,  # OpenCodeDriver
    api_token: str = "",
    port: int = 8901,
) -> web.AppRunner:
    """Start the HTTP server (OpenCode mode). Returns runner for lifecycle management."""
    global _startup_time
    _startup_time = time.monotonic()

    app = create_opencode_app(driver=driver, api_token=api_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("HTTP inject server (opencode) started on 127.0.0.1:%d", port)
    return runner
