"""Echo test backend — mirrors agentgate-backend HTTP API.

收到 inject 消息后根据触发词产生不同回复，用于 QA 验收网关功能，
不依赖 tmux / Claude Code。

CLI: echo-backend --port 8901 --token echo-test-token
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

import click
from aiohttp import web

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

class EchoState:
    """All in-memory state for the echo backend."""

    def __init__(self) -> None:
        self._start_time = time.monotonic()

        # message_id → injected_at ISO + text_hint (for /api/unprocessed)
        self._pending: dict[str, dict] = {}  # message_id → {injected_at, text_hint}

        # seen message_ids for idempotency (message_id → delivery_id)
        self._seen: dict[str, str] = {}

        # output ring: list of message dicts (role, text, content_type, timestamp)
        # each message appended here, "since" is index-based
        self._output: list[dict] = []

        # lock protects _pending, _seen, _output
        self._lock = asyncio.Lock()

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    async def inject(self, text: str, message_id: str, sender_name: str) -> dict:
        """Record a message and enqueue echo reply. Returns inject response dict."""
        async with self._lock:
            if message_id in self._seen:
                delivery_id = self._seen[message_id]
                return {
                    "ok": True,
                    "delivery_id": "dup",
                    "window_id": "@echo",
                    "msg": "Duplicate message_id, skipped",
                }

            delivery_id = uuid.uuid4().hex[:12]
            self._seen[message_id] = delivery_id

            injected_at = _now_iso()
            self._pending[message_id] = {
                "message_id": message_id,
                "injected_at": injected_at,
                "text_hint": text[:80],
            }

            # Schedule reply production asynchronously so inject returns fast
            asyncio.create_task(self._produce_replies(text, message_id))

            return {
                "ok": True,
                "delivery_id": delivery_id,
                "window_id": "@echo",
                "msg": "Sent to echo",
            }

    async def _produce_replies(self, text: str, message_id: str) -> None:
        """Produce echo reply messages based on trigger words."""
        replies = _build_replies(text)

        # Extract delay trigger first
        delay = _parse_delay(text)
        if delay > 0:
            await asyncio.sleep(delay)

        async with self._lock:
            for reply in replies:
                self._output.append(reply)

    async def get_output(self, since: int) -> dict:
        """Return incremental output since the given index."""
        async with self._lock:
            messages = self._output[since:]
            next_offset = len(self._output)
            return {
                "ok": True,
                "window_name": "echo",
                "messages": list(messages),
                "count": len(messages),
                "since": since,
                "next_offset": next_offset,
            }

    async def confirm_processed(self, message_ids: list[str]) -> int:
        """Mark message_ids as processed; remove from pending."""
        async with self._lock:
            confirmed = 0
            for mid in message_ids:
                if mid in self._pending:
                    del self._pending[mid]
                    confirmed += 1
            return confirmed

    async def get_unprocessed(self) -> list[dict]:
        """Return all pending (unprocessed) messages."""
        async with self._lock:
            return list(self._pending.values())


# ---------------------------------------------------------------------------
# Reply builders
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text_msg(text: str) -> dict:
    return {
        "role": "assistant",
        "text": text,
        "content_type": "text",
        "timestamp": _now_iso(),
    }


def _thinking_msg(text: str) -> dict:
    return {
        "role": "assistant",
        "text": text,
        "content_type": "thinking",
        "timestamp": _now_iso(),
    }


def _parse_delay(text: str) -> float:
    """Extract delay seconds from [test-delay:Ns] trigger. Returns 0 if not found."""
    import re
    m = re.search(r'\[test-delay:(\d+(?:\.\d+)?)s\]', text)
    if m:
        return float(m.group(1))
    return 0.0


def _parse_long(text: str) -> int:
    """Extract N from [test-long:N] trigger. Returns 0 if not found."""
    import re
    m = re.search(r'\[test-long:(\d+)\]', text)
    if m:
        return int(m.group(1))
    return 0


def _build_replies(text: str) -> list[dict]:
    """Build reply messages based on trigger words in text.

    Multiple triggers can be combined; each produces its own output.
    If no trigger matches, produce plain echo.
    """
    replies = []
    triggered = False

    if "[test-thinking]" in text:
        triggered = True
        replies.append(_thinking_msg(
            "Let me think about this... "
            "The user sent a message with [test-thinking] trigger. "
            "I should produce a thinking entry followed by a text entry."
        ))
        replies.append(_text_msg("Echo (thinking done): " + text.replace("[test-thinking]", "").strip()))

    if "[test-markdown]" in text:
        triggered = True
        md = (
            "# Markdown 测试\n\n"
            "这是一段**加粗文字**和`内联代码`。\n\n"
            "```python\n"
            "def hello():\n"
            "    print('Hello, World!')\n"
            "```\n\n"
            "列表示例：\n"
            "- 第一项\n"
            "- 第二项\n"
            "- 第三项\n\n"
            "> 这是一段引用文字。\n\n"
            "测试完成。"
        )
        replies.append(_text_msg(md))

    long_n = _parse_long(text)
    if long_n > 0:
        triggered = True
        # Generate a text of approximately N characters
        base = "这是一段用于测试长消息分割的测试文本。"
        repeats = (long_n // len(base)) + 1
        long_text = (base * repeats)[:long_n]
        replies.append(_text_msg(long_text))

    # [test-delay] doesn't produce a separate reply, handled by delay logic
    # But if ONLY delay trigger present, we still need an echo
    if "[test-delay:" in text:
        # Delay is handled in _produce_replies; we produce a simple echo
        # but only if no other trigger was set
        if not triggered:
            triggered = True
            clean = _strip_trigger(text, "[test-delay:")
            replies.append(_text_msg("Echo (delayed): " + clean.strip()))

    if not triggered:
        replies.append(_text_msg("Echo: " + text))

    return replies


def _strip_trigger(text: str, prefix: str) -> str:
    """Strip a trigger that starts with prefix (up to closing ])."""
    import re
    pattern = r'\[' + re.escape(prefix.lstrip('[')) + r'[^\]]*\]'
    return re.sub(pattern, '', text)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_auth(request: web.Request, token: str) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer "):] == token


def _forbidden() -> web.Response:
    return web.json_response(
        {"ok": False, "error": "forbidden", "msg": "Invalid or missing token"},
        status=403,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    """/api/health — no auth required."""
    state: EchoState = request.app["state"]
    return web.json_response({
        "status": "ok",
        "windows": [
            {
                "window_id": "@echo",
                "window_name": "echo",
                "pane_command": "echo",
                "session_id": "echo-session",
                "pending_deliveries": 0,
            }
        ],
        "uptime_seconds": state.uptime_seconds,
        "watchdog_enabled": False,
        "window_health": {
            "@echo": {"status": "ok", "detail": "echo backend running"},
        },
    })


async def handle_inject(request: web.Request) -> web.Response:
    """/api/inject — inject a message."""
    token: str = request.app["token"]
    if not _check_auth(request, token):
        return _forbidden()

    try:
        body = await request.json()
    except Exception:
        logger.warning("handle_inject: invalid JSON body from %s", request.remote, exc_info=True)
        return web.json_response(
            {"ok": False, "error": "bad_request", "msg": "Invalid JSON body"},
            status=400,
        )

    text = body.get("text", "")
    message_id = body.get("message_id") or uuid.uuid4().hex
    sender_name = body.get("sender_name", "")

    if not text:
        return web.json_response(
            {"ok": False, "error": "bad_request", "msg": "Field 'text' is required"},
            status=400,
        )

    state: EchoState = request.app["state"]
    result = await state.inject(text, message_id, sender_name)
    return web.json_response(result)


async def handle_output(request: web.Request) -> web.Response:
    """/api/output/{window_name}?since={offset}"""
    token: str = request.app["token"]
    if not _check_auth(request, token):
        return _forbidden()

    try:
        since = int(request.rel_url.query.get("since", 0))
    except (TypeError, ValueError):
        since = 0

    state: EchoState = request.app["state"]
    result = await state.get_output(since)
    return web.json_response(result)


async def handle_confirm_processed(request: web.Request) -> web.Response:
    """/api/confirm_processed"""
    token: str = request.app["token"]
    if not _check_auth(request, token):
        return _forbidden()

    try:
        body = await request.json()
    except Exception:
        logger.warning("handle_confirm_processed: invalid JSON body from %s", request.remote, exc_info=True)
        return web.json_response(
            {"ok": False, "error": "bad_request", "msg": "Invalid JSON body"},
            status=400,
        )

    message_ids = body.get("message_ids", [])
    if not isinstance(message_ids, list):
        return web.json_response(
            {"ok": False, "error": "bad_request", "msg": "'message_ids' must be a list"},
            status=400,
        )

    state: EchoState = request.app["state"]
    confirmed = await state.confirm_processed(message_ids)
    return web.json_response({"ok": True, "confirmed": confirmed})


async def handle_unprocessed(request: web.Request) -> web.Response:
    """/api/unprocessed"""
    token: str = request.app["token"]
    if not _check_auth(request, token):
        return _forbidden()

    state: EchoState = request.app["state"]
    messages = await state.get_unprocessed()
    return web.json_response({"ok": True, "messages": messages})


async def handle_window(request: web.Request) -> web.Response:
    """/api/window — stub, always returns success."""
    token: str = request.app["token"]
    if not _check_auth(request, token):
        return _forbidden()

    return web.json_response({
        "ok": True,
        "window_name": "echo",
        "window_id": "@echo",
        "work_dir": "/tmp",
    })


# ---------------------------------------------------------------------------
# App factory + CLI
# ---------------------------------------------------------------------------

def make_app(token: str) -> web.Application:
    app = web.Application()
    app["token"] = token
    app["state"] = EchoState()

    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/inject", handle_inject)
    app.router.add_get("/api/output/{window_name}", handle_output)
    app.router.add_post("/api/confirm_processed", handle_confirm_processed)
    app.router.add_get("/api/unprocessed", handle_unprocessed)
    app.router.add_post("/api/window", handle_window)

    return app


@click.command()
@click.option("--port", default=8901, type=int, show_default=True, help="Port to listen on")
@click.option("--token", default="echo-test-token", show_default=True, help="Bearer token for auth")
@click.option("--log-level", default="INFO", show_default=True, help="Logging level")
def main(port: int, token: str, log_level: str) -> None:
    """Echo test backend — mirrors agentgate-backend HTTP API."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("Starting echo-backend on port %d", port)
    app = make_app(token)
    web.run_app(app, port=port, access_log=None)


if __name__ == "__main__":
    main()
