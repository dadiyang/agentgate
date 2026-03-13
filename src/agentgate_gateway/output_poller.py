"""Output polling from backends and outbound push to channel adapters."""

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone

import httpx

from agentgate_gateway.db import MessageDB
from agentgate_gateway.formatter import format_for_channel
from agentgate_gateway.router import Router
from agentgate_gateway.splitter import split_message

logger = logging.getLogger(__name__)

PUSH_MAX_RETRY = 5
PUSH_RETRY_DELAYS = [2, 4, 8, 16, 30]  # exponential backoff seconds


class OutputPoller:
    def __init__(
        self,
        db: MessageDB,
        router: Router,
        backends: dict,
        adapters: dict,
        poll_interval: float = 2.0,
        alert_manager=None,
    ):
        """
        backends: dict of backend_id -> object with .url, .api_token, .status attributes
        adapters: dict of channel_type -> ChannelAdapter
        alert_manager: optional AlertManager for push failure notifications
        """
        self._db = db
        self._router = router
        self._backends = backends
        self._adapters = adapters
        self._poll_interval = poll_interval
        self._alert_manager = alert_manager
        self._offsets: dict[str, int] = {}  # backend_id -> byte offset
        self._http = httpx.AsyncClient(timeout=10)
        self._running = True

    async def close(self):
        await self._http.aclose()

    async def run(self):
        """Main polling loop."""
        while self._running:
            for backend_id, backend in list(self._backends.items()):
                status = getattr(backend, "status", None)
                if status is None and isinstance(backend, dict):
                    status = backend.get("status", "unknown")
                if status == "unhealthy":
                    continue
                try:
                    await self._poll_backend(backend_id, backend)
                except Exception as e:
                    logger.error("Poll %s failed: %s", backend_id, e, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _poll_backend(self, backend_id: str, backend):
        url = getattr(backend, "url", None) or (
            backend.get("url", "") if isinstance(backend, dict) else ""
        )
        token = getattr(backend, "api_token", None) or (
            backend.get("api_token", "") if isinstance(backend, dict) else ""
        )

        offset = self._offsets.get(backend_id, 0)
        resp = await self._http.get(
            f"{url}/api/output/main?since={offset}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("ok"):
            return

        next_offset = data.get("next_offset", offset)

        # Detect backend restart: next_offset < current offset means the
        # backend's output counter was reset (Bug #9). Re-poll from 0.
        if next_offset < offset:
            logger.warning(
                "Backend %s output counter reset detected "
                "(next_offset=%d < since=%d) — re-polling from 0",
                backend_id, next_offset, offset,
            )
            self._offsets[backend_id] = 0
            # Re-fetch from offset 0 to pick up any output generated after restart
            resp = await self._http.get(
                f"{url}/api/output/main?since=0",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            if not data.get("ok"):
                return
            next_offset = data.get("next_offset", 0)

        if data.get("count", 0) == 0:
            self._offsets[backend_id] = next_offset
            return

        self._offsets[backend_id] = next_offset

        # E-1: Confirm processed messages on backend after getting new output
        await self._confirm_processed(backend_id, url, token)

        # Filter thinking blocks (AC-12): only pass through text messages
        text_messages = [
            m for m in data["messages"] if m.get("content_type") == "text"
        ]
        if not text_messages:
            return

        # Combine all text segments
        combined = "\n\n".join(m["text"] for m in text_messages)

        # Reverse route: find all bound channels for this backend
        bindings = self._router.reverse_lookup(backend_id)
        for channel_type, bot_id, group_id in bindings:
            await self._push_to_channel(backend_id, channel_type, group_id, combined)

    async def _confirm_processed(self, backend_id: str, url: str, token: str):
        """Confirm all pending inbound messages for this backend as processed.

        Called after new agent output is detected — new output implies agent
        processed pending input. Updates both backend and gateway DB.
        """
        try:
            # Get unprocessed messages from backend
            resp = await self._http.get(
                f"{url}/api/unprocessed",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            if not data.get("ok"):
                return
            unprocessed = data.get("unprocessed", [])
            if not unprocessed:
                return

            message_ids = [m["message_id"] for m in unprocessed]

            # Confirm on backend
            resp = await self._http.post(
                f"{url}/api/confirm_processed",
                json={"message_ids": message_ids},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.warning("confirm_processed failed for %s: %d", backend_id, resp.status_code)

            # Update gateway DB: mark these messages as processed
            now = datetime.now(timezone.utc).isoformat()
            for mid in message_ids:
                try:
                    await self._db.update_inbound_process(mid, "processed", processed_at=now)
                except Exception as e:
                    logger.error("Failed to update process_status for %s: %s", mid, e, exc_info=True)

        except Exception as e:
            logger.error("confirm_processed error for %s: %s", backend_id, e, exc_info=True)

    async def _push_to_channel(
        self, backend_id: str, channel_type: str, group_id: str, text: str
    ):
        # Split raw text FIRST, then format each part for the channel.
        # Formatting (e.g. Feishu JSON) can change size and structure;
        # splitting after formatting breaks structured output (Bug #5).
        parts_raw = split_message(text, channel_type)
        parts = [format_for_channel(channel_type, p) for p in parts_raw]

        for i, part in enumerate(parts):
            # Include shard_index in hash to avoid dedup collision when
            # different shards have identical content (Bug #6).
            content_hash = hashlib.sha256(
                f"{backend_id}:{i}:{part}:{self._offsets.get(backend_id, 0)}".encode()
            ).hexdigest()
            msg_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()

            # E-7: Dedup check — skip if same content already pushed for this backend
            if await self._db.has_outbound_content_hash(backend_id, content_hash):
                logger.debug("Outbound dedup: content_hash=%s already exists, skipping", content_hash[:16])
                continue

            # Persist BEFORE push (crash safety)
            await self._db.save_outbound(
                {
                    "id": msg_id,
                    "fetched_at": now,
                    "backend_id": backend_id,
                    "channel_type": channel_type,
                    "group_id": group_id,
                    "content": part,
                    "shard_index": i + 1,
                    "shard_total": len(parts),
                    "content_hash": content_hash,
                }
            )

            # Push to channel with retry
            adapter = self._adapters.get(channel_type)
            if not adapter:
                logger.warning("No adapter for channel %s", channel_type)
                await self._db.update_outbound_push(
                    msg_id, "failed", error_message=f"No adapter for channel {channel_type}"
                )
                continue

            await self._push_with_retry(adapter, msg_id, group_id, part)

    async def _push_with_retry(self, adapter, msg_id: str, group_id: str, text: str):
        """Push a single message shard with exponential backoff retry."""
        for attempt in range(PUSH_MAX_RETRY):
            try:
                success = await adapter.send_message(group_id, text)
                if success:
                    pushed_at = datetime.now(timezone.utc).isoformat()
                    await self._db.update_outbound_push(msg_id, "pushed", pushed_at=pushed_at)
                    return
                logger.warning(
                    "Push attempt %d/%d failed: send_message returned False (msg_id=%s)",
                    attempt + 1, PUSH_MAX_RETRY, msg_id,
                )
            except Exception as e:
                logger.error(
                    "Push attempt %d/%d error (msg_id=%s): %s",
                    attempt + 1, PUSH_MAX_RETRY, msg_id, e, exc_info=True,
                )

            # Update retry_count in DB (E-5)
            await self._db.increment_outbound_retry(msg_id)

            if attempt < PUSH_MAX_RETRY - 1:
                await asyncio.sleep(PUSH_RETRY_DELAYS[attempt])

        # All retries exhausted
        await self._db.update_outbound_push(
            msg_id, "failed", error_message=f"{PUSH_MAX_RETRY} push retries exhausted"
        )
        # E-3: Alert on push failure
        if self._alert_manager:
            try:
                await self._alert_manager.send(
                    "outbound_push_failed", "WARNING",
                    f"出站消息推送 {PUSH_MAX_RETRY} 次重试后失败 (msg_id={msg_id})",
                    group_id,
                )
            except Exception as e:
                logger.error("Alert send failed: %s", e, exc_info=True)

    async def repush_message(self, msg: dict):
        """Re-push a previously persisted outbound message (for recovery)."""
        adapter = self._adapters.get(msg["channel_type"])
        if not adapter:
            return
        success = await adapter.send_message(msg["group_id"], msg["content"])
        if success:
            pushed_at = datetime.now(timezone.utc).isoformat()
            await self._db.update_outbound_push(msg["id"], "pushed", pushed_at=pushed_at)

    def reset_offset(self, backend_id: str):
        """Reset the polling offset for a backend (e.g. after restart)."""
        old = self._offsets.pop(backend_id, 0)
        if old > 0:
            logger.info("Reset output offset for backend %s: %d → 0", backend_id, old)

    def stop(self):
        self._running = False
