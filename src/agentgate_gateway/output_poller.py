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


class OutputPoller:
    def __init__(
        self,
        db: MessageDB,
        router: Router,
        backends: dict,
        adapters: dict,
        poll_interval: float = 2.0,
    ):
        """
        backends: dict of backend_id -> object with .url, .api_token, .status attributes
        adapters: dict of channel_type -> ChannelAdapter
        """
        self._db = db
        self._router = router
        self._backends = backends
        self._adapters = adapters
        self._poll_interval = poll_interval
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
        if not data.get("ok") or data.get("count", 0) == 0:
            return

        self._offsets[backend_id] = data.get("next_offset", offset)

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

    async def _push_to_channel(
        self, backend_id: str, channel_type: str, group_id: str, text: str
    ):
        # Format for target channel
        formatted = format_for_channel(channel_type, text)
        # Split if message exceeds channel limit
        parts = split_message(formatted, channel_type)

        for i, part in enumerate(parts):
            content_hash = hashlib.sha256(
                f"{backend_id}:{part}:{self._offsets.get(backend_id, 0)}".encode()
            ).hexdigest()
            msg_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()

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

            # Push to channel
            adapter = self._adapters.get(channel_type)
            if not adapter:
                logger.warning("No adapter for channel %s", channel_type)
                await self._db.update_outbound_push(
                    msg_id, "failed", error_message=f"No adapter for channel {channel_type}"
                )
                continue

            success = await adapter.send_message(group_id, part)
            if success:
                pushed_at = datetime.now(timezone.utc).isoformat()
                await self._db.update_outbound_push(msg_id, "pushed", pushed_at=pushed_at)
            else:
                await self._db.update_outbound_push(
                    msg_id, "failed", error_message="send_message returned False"
                )

    async def repush_message(self, msg: dict):
        """Re-push a previously persisted outbound message (for recovery)."""
        adapter = self._adapters.get(msg["channel_type"])
        if not adapter:
            return
        success = await adapter.send_message(msg["group_id"], msg["content"])
        if success:
            pushed_at = datetime.now(timezone.utc).isoformat()
            await self._db.update_outbound_push(msg["id"], "pushed", pushed_at=pushed_at)

    def stop(self):
        self._running = False
