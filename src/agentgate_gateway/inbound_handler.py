"""Inbound message pipeline: channel callback → dedup → persist → route → inject backend."""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from opentelemetry import trace

from agentgate_gateway.db import MessageDB
from agentgate_gateway.router import Router

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

DELIVERY_TIMEOUT = 30  # seconds
MAX_RETRY = 3
RETRY_DELAYS = [5, 10, 15]


class InboundHandler:
    def __init__(
        self,
        db: MessageDB,
        router: Router,
        backends: dict,
        adapters: dict,
        alert_manager=None,
    ):
        """
        backends: dict of backend_id -> object with .url, .api_token attributes
        adapters: dict of channel_type -> ChannelAdapter
        alert_manager: optional AlertManager for failure notifications
        """
        self._db = db
        self._router = router
        self._backends = backends
        self._adapters = adapters
        self._alert_manager = alert_manager
        self._http = httpx.AsyncClient(timeout=DELIVERY_TIMEOUT)

    async def close(self):
        await self._http.aclose()

    async def handle_message(
        self,
        channel_type: str,
        bot_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        group_name: str,
        text: str,
        dedup_key: str,
        target_backend_id: str | None = None,
        action_id: str | None = None,
        fire_and_forget: bool = False,
    ):
        """Called by channel adapters when a message arrives.

        target_backend_id: When set (e.g. HTTP channel), skip route matching
        and inject directly to the specified backend.
        action_id: When set, use this ID instead of generating a new UUID.
        fire_and_forget: When True, persist the message and start injection
        in a background task instead of awaiting it.  Used by the HTTP inject
        endpoint so the caller gets a fast response.
        """
        with _tracer.start_as_current_span(
            "inbound",
            attributes={
                "channel": channel_type,
                "bot_id": bot_id,
                "chat_id": chat_id,
            },
        ):
            return await self._handle_message_inner(
                channel_type,
                bot_id,
                chat_id,
                sender_id,
                sender_name,
                group_name,
                text,
                dedup_key,
                target_backend_id,
                action_id,
                fire_and_forget,
            )

    async def _handle_message_inner(
        self,
        channel_type,
        bot_id,
        chat_id,
        sender_id,
        sender_name,
        group_name,
        text,
        dedup_key,
        target_backend_id,
        action_id,
        fire_and_forget,
    ):
        logger.info(
            "Inbound: channel=%s bot=%s chat=%s sender=%s text=%s",
            channel_type,
            bot_id,
            chat_id,
            sender_name,
            text[:80],
        )

        # 1. Dedup check (3-layer idempotency: channel level)
        if await self._db.has_dedup_key(dedup_key):
            logger.info("Duplicate message ignored: dedup_key=%s", dedup_key)
            return

        # 2. Route match — HTTP channel passes target_backend_id directly
        if target_backend_id:
            backend_id = target_backend_id
            logger.info("Inbound routed (direct): backend=%s", backend_id)
        else:
            backend_id = self._router.match(channel_type, bot_id, chat_id)
            if backend_id:
                logger.info(
                    "Inbound routed: (%s, %s, %s) → %s",
                    channel_type,
                    bot_id,
                    chat_id,
                    backend_id,
                )
            if not backend_id:
                logger.warning(
                    "No route matched — message dropped. "
                    "channel=%s bot_id=%s chat_id=%s sender=%s "
                    "(add a route in config.yaml to handle this group)",
                    channel_type,
                    bot_id,
                    chat_id,
                    sender_name,
                )
                return

        # 3. Persist BEFORE processing (crash safety)
        msg_id = str(uuid.uuid4())
        action_id = action_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.save_inbound(
            {
                "id": msg_id,
                "action_id": action_id,
                "received_at": now,
                "channel_type": channel_type,
                "channel_bot_id": bot_id,
                "chat_id": chat_id,
                "group_name": group_name,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "content": text,
                "backend_id": backend_id,
                "dedup_key": dedup_key,
            }
        )

        # 4. Inject to backend with retry
        if fire_and_forget:
            asyncio.create_task(
                self._inject_with_retry(
                    msg_id, backend_id, text, sender_name, channel_type, chat_id
                )
            )
        else:
            await self._inject_with_retry(
                msg_id, backend_id, text, sender_name, channel_type, chat_id
            )

    async def _inject_with_retry(
        self,
        msg_id: str,
        backend_id: str,
        text: str,
        sender_name: str,
        channel_type: str,
        chat_id: str,
    ):
        backend = self._backends.get(backend_id)
        if not backend:
            logger.error("Backend %s not configured", backend_id)
            await self._db.update_status(
                msg_id, "failed", error_message=f"Backend {backend_id} not configured"
            )
            return

        url = backend.url if hasattr(backend, "url") else backend.get("url", "")
        token = (
            backend.api_token
            if hasattr(backend, "api_token")
            else backend.get("api_token", "")
        )
        window_name = (
            backend.default_window
            if hasattr(backend, "default_window")
            else backend.get("default_window", "main")
        )

        for attempt in range(MAX_RETRY):
            try:
                t0 = time.monotonic()
                resp = await self._http.post(
                    f"{url}/api/inject",
                    json={
                        "window_name": window_name,
                        "text": text,
                        "action_id": msg_id,
                        "sender_name": sender_name,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                elapsed_ms = (time.monotonic() - t0) * 1000
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        logger.info(
                            "Inject ok: backend=%s msg_id=%s delivery_id=%s elapsed=%.0fms",
                            backend_id,
                            msg_id,
                            data.get("delivery_id"),
                            elapsed_ms,
                        )
                        await self._db.update_status(msg_id, "delivered")
                        return
                logger.warning(
                    "Inject attempt %d/%d failed: status=%d body=%s",
                    attempt + 1,
                    MAX_RETRY,
                    resp.status_code,
                    resp.text[:200],
                )
            except Exception as e:
                logger.error(
                    "Inject attempt %d/%d error: %s: %s",
                    attempt + 1,
                    MAX_RETRY,
                    type(e).__name__,
                    e or "(no detail)",
                    exc_info=True,
                )

            # E-5: Update retry_count in DB
            await self._db.increment_retry(msg_id)

            if attempt < MAX_RETRY - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])

        # All retries exhausted
        await self._db.update_status(
            msg_id, "failed", error_message="3 retries exhausted"
        )
        # E-3: Alert on delivery failure
        if self._alert_manager:
            try:
                await self._alert_manager.send(
                    "inbound_delivery_failed",
                    "WARNING",
                    f"消息送达 {MAX_RETRY} 次重试后失败 (backend={backend_id}, msg_id={msg_id})",
                    backend_id,
                )
            except Exception as e:
                logger.error("Alert send failed: %s", e, exc_info=True)
        # AC-29: Notify user in the IM group
        adapter = self._adapters.get(channel_type)
        if adapter:
            try:
                await adapter.send_message(
                    chat_id,
                    "⚠️ 消息暂时无法处理，系统正在恢复中。请稍后重试或联系管理员。",
                )
            except Exception as e:
                logger.error("Failed to notify user: %s", e, exc_info=True)

    async def reinject_message(self, msg: dict):
        """Re-inject a previously persisted message (for crash recovery)."""
        await self._inject_with_retry(
            msg["id"],
            msg["backend_id"],
            msg["content"],
            msg.get("sender_name", ""),
            msg.get("channel_type", ""),
            msg.get("chat_id", ""),
        )
