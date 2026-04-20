"""DingTalk channel adapter — Stream mode WebSocket (no public IP required).

Uses the official dingtalk-stream SDK to receive messages via a long-lived
WebSocket connection to DingTalk's servers.

Replies are sent via the per-message sessionWebhook URL.  The webhook is
cached by conversation_id and expires after ~1 hour; for typical agent
workflows (reply within minutes) this is sufficient.

Audio messages: DingTalk performs server-side ASR and provides the
transcription in the `recognition` field — no local ASR needed.

Config (gateway config.yaml):
  channels:
    dingtalk:
      bots:
        - client_id: "dingxxxxxxx"
          client_secret: "xxxxxxxxxx"
          bot_id: "my-dingtalk-bot"   # used as bot_id in routes
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)

# sessionWebhook validity margin: stop using a webhook 5 min before it expires
_WEBHOOK_EXPIRY_MARGIN_MS = 5 * 60 * 1000


class DingTalkAdapter(ChannelAdapter):
    """ChannelAdapter for DingTalk via Stream WebSocket."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        on_message: OnMessageCallback,
        bot_id: str = "",
        allow_from: str = "*",
    ):
        super().__init__(name="dingtalk", on_message=on_message)
        self._client_id = client_id
        self._client_secret = client_secret
        self._bot_id = bot_id or client_id
        self._allow_from = allow_from  # "*" or comma-separated staffId list

        # conversation_id → (session_webhook_url, expires_at_ms)
        self._webhook_cache: dict[str, tuple[str, int]] = {}

        self._stream_task: asyncio.Task | None = None
        self._connected = False

    # --- ChannelAdapter interface ---

    async def start(self) -> None:
        try:
            from dingtalk_stream import DingTalkStreamClient, Credential, chatbot
        except ImportError:
            raise RuntimeError(
                "dingtalk-stream is required for DingTalk support: pip install dingtalk-stream"
            )

        credential = Credential(self._client_id, self._client_secret)
        client = DingTalkStreamClient(credential)

        handler = _BotMessageHandler(self._on_bot_message)
        client.register_callback_handler(chatbot.ChatbotMessage.TOPIC, handler)

        logger.info("DingTalk adapter starting (bot_id=%s)", self._bot_id)
        self._stream_task = asyncio.create_task(self._run_stream(client))

    async def stop(self) -> None:
        self._connected = False
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        logger.info("DingTalk adapter stopped (bot_id=%s)", self._bot_id)

    async def _real_send_message(self, chat_id: str, text: str) -> bool:
        """Send a Markdown message via the cached sessionWebhook for this conversation."""
        webhook_url, expires_at_ms = self._webhook_cache.get(chat_id, ("", 0))
        now_ms = int(time.time() * 1000)

        if not webhook_url:
            logger.error(
                "DingTalk send failed: no webhook cached for conversation_id=%s "
                "(no inbound message received yet from this chat)",
                chat_id,
            )
            return False

        if now_ms >= expires_at_ms - _WEBHOOK_EXPIRY_MARGIN_MS:
            logger.error(
                "DingTalk send failed: webhook expired for conversation_id=%s "
                "(expiry=%d now=%d) — agent took too long to respond",
                chat_id,
                expires_at_ms,
                now_ms,
            )
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "reply", "text": _prepare_markdown(text)},
        }
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.info(
                "DingTalk send ok [%s]: conversation_id=%s elapsed=%.0fms",
                self._bot_id,
                chat_id,
                elapsed_ms,
            )
            return True
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "DingTalk send failed [%s]: conversation_id=%s %s elapsed=%.0fms",
                self._bot_id,
                chat_id,
                e,
                elapsed_ms,
                exc_info=True,
            )
            return False

    def _real_is_connected(self) -> bool:
        return self._connected

    # --- Internal ---

    async def _run_stream(self, client: Any) -> None:
        """Run the Stream client — it handles reconnect internally."""
        try:
            self._connected = True
            await client.start()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("DingTalk stream client crashed: %s", e, exc_info=True)
        finally:
            self._connected = False

    async def _on_bot_message(self, msg: Any, raw_data: dict | None = None) -> None:
        """Called by _BotMessageHandler for each incoming message."""
        from dingtalk_stream import chatbot

        # Cache the session webhook immediately (valid for ~1 hour)
        conversation_id = msg.conversation_id or ""
        if msg.session_webhook and conversation_id:
            expires_at = msg.session_webhook_expired_time or 0
            self._webhook_cache[conversation_id] = (msg.session_webhook, expires_at)
            logger.debug(
                "DingTalk webhook cached: conversation_id=%s expires_at=%d",
                conversation_id,
                expires_at,
            )

        # Permission check
        if not self._is_allowed(msg.sender_staff_id or ""):
            logger.debug(
                "DingTalk ignored (not in allow_from): sender_staff_id=%s",
                msg.sender_staff_id,
            )
            return

        # Extract text — handle text, audio, and richText messages
        msg_type = msg.message_type or ""
        if msg_type == "audio":
            # DingTalk performs ASR server-side; use the recognition field directly
            try:
                content = msg.extensions.get("content", "{}") if msg.extensions else {}
                raw_content = (
                    content if isinstance(content, dict) else json.loads(content)
                )
                text = raw_content.get("recognition", "")
            except json.JSONDecodeError:
                text = ""
            except (KeyError, TypeError):
                text = ""
            if not text:
                logger.warning(
                    "DingTalk audio message with no recognition: message_id=%s",
                    msg.message_id,
                )
                return
        elif msg_type == "text":
            text = (msg.text.content if msg.text else "") or ""
        elif msg_type == "richText":
            # Rich text is an array of {"text": "..."} segments; concatenate them all
            try:
                content = (raw_data or {}).get("content", {})
                segments = content.get("richText", [])
                text = "".join(seg.get("text", "") for seg in segments)
            except (AttributeError, TypeError):
                text = ""
            if not text.strip():
                logger.warning(
                    "DingTalk richText message with no extractable text: message_id=%s",
                    msg.message_id,
                )
                return
        else:
            logger.debug("DingTalk: unsupported message_type=%s, skipping", msg_type)
            return

        if not text.strip():
            return

        sender_id = msg.sender_staff_id or msg.sender_id or ""
        sender_name = msg.sender_nick or ""
        group_name = msg.conversation_title or ""
        dedup_key = msg.message_id or ""

        logger.info(
            "DingTalk inbound [%s]: conversation_id=%s sender=%s msg_type=%s text=%s",
            self._bot_id,
            conversation_id,
            sender_name,
            msg_type,
            text[:80],
        )

        if self._on_message:
            await self._on_message(
                "dingtalk",
                self._bot_id,
                conversation_id,
                sender_id,
                sender_name,
                group_name,
                text,
                dedup_key,
            )

    def _is_allowed(self, staff_id: str) -> bool:
        if not self._allow_from or self._allow_from == "*":
            return True
        allowed = {s.strip() for s in self._allow_from.split(",")}
        return staff_id in allowed


class _BotMessageHandler:
    """Wraps the async on_message callback in the SDK's CallbackHandler interface."""

    def __init__(self, on_message_fn):
        self._on_message_fn = on_message_fn
        self.dingtalk_client = None
        self.logger = logging.getLogger(__name__)

    def pre_start(self):
        pass

    async def process(self, message):
        from dingtalk_stream import AckMessage, chatbot

        try:
            logger.info("DingTalk raw message: %r", message.data)

            # message.data is already a dict from the SDK, but handle both cases
            if isinstance(message.data, str):
                data = json.loads(message.data)
            else:
                data = message.data
            bot_msg = chatbot.ChatbotMessage.from_dict(data)
            await self._on_message_fn(bot_msg, data)
        except Exception as e:
            logger.error("DingTalk handler error: %s", e, exc_info=True)

        return AckMessage.STATUS_OK, "OK"

    async def raw_process(self, callback_message):
        from dingtalk_stream import AckMessage
        from dingtalk_stream.frames import Headers

        code, message = await self.process(callback_message)
        ack = AckMessage()
        ack.code = code
        ack.headers.message_id = callback_message.headers.message_id
        ack.headers.content_type = Headers.CONTENT_TYPE_APPLICATION_JSON
        ack.data = {"response": message}
        return ack


def _prepare_markdown(text: str) -> str:
    """Ensure line breaks render correctly in DingTalk Markdown.

    DingTalk requires two trailing spaces or a blank line between paragraphs
    to force a line break. Also replaces leading spaces with non-breaking
    spaces to prevent Markdown from stripping indentation.
    """
    lines = text.split("\n")
    in_code_block = False
    result = []

    for i, line in enumerate(lines):
        trimmed = line.lstrip()
        if trimmed.startswith("```"):
            in_code_block = not in_code_block

        if not in_code_block:
            # Preserve leading spaces as non-breaking spaces
            leading = len(line) - len(trimmed)
            if leading:
                line = "\u00a0" * leading + trimmed

        is_last = i == len(lines) - 1
        next_nonempty = any(lines[j].strip() for j in range(i + 1, len(lines)))

        if not is_last and line.strip() and next_nonempty and not in_code_block:
            result.append(line + "  ")
        else:
            result.append(line)

    return "\n".join(result)
