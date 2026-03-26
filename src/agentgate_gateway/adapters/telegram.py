import asyncio
import logging
import os
import time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)


class TelegramAdapter(ChannelAdapter):
    def __init__(
        self,
        bot_token: str,
        on_message: OnMessageCallback,
        proxy: str = "",
        bot_id_override: str = "",
        mention_only: bool = False,
    ):
        super().__init__(name="telegram", on_message=on_message)
        self._bot_token = bot_token
        self._bot_id_override = bot_id_override
        self._mention_only = mention_only
        proxy = proxy or os.environ.get("HTTPS_PROXY", "")
        # Increase pool_timeout (default 1.0s) to prevent Pool timeout during
        # shutdown when multiple bots compete for connections to commit offsets.
        proxy_arg = proxy or None
        request = HTTPXRequest(pool_timeout=10.0, proxy=proxy_arg)
        get_updates_request = HTTPXRequest(pool_timeout=10.0, proxy=proxy_arg)
        builder = (
            ApplicationBuilder()
            .token(bot_token)
            .request(request)
            .get_updates_request(get_updates_request)
        )
        self._app = builder.build()
        self._connected = False
        self._bot_username = ""

    async def start(self):
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, self._handle_message
            )
        )
        # Catch-all: log non-text updates (photos, stickers, edited messages, etc.)
        # that bypass the TEXT filter — helps diagnose "message not received" issues
        async def _catchall(update: Update, _context: ContextTypes.DEFAULT_TYPE):
            logger.debug(
                "TG non-text update [%s]: update_id=%s types=%s",
                self._bot_username, update.update_id,
                [k for k in ("message", "edited_message", "channel_post", "callback_query")
                 if getattr(update, k, None) is not None],
            )
        self._app.add_handler(MessageHandler(filters.ALL, _catchall), group=1)
        await self._app.initialize()
        me = await self._app.bot.get_me()
        self._bot_username = self._bot_id_override or me.username or ""
        await self._app.start()
        await self._app.updater.start_polling()
        self._connected = True
        logger.info("Telegram bot started: %s", self._bot_username)

    async def stop(self):
        self._connected = False
        if self._app.updater and self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user = update.effective_message.from_user if update.effective_message else None
        logger.info(
            "TG inbound [%s]: update_id=%s chat_id=%s sender=%s text=%s",
            self._bot_username,
            update.update_id,
            update.effective_chat.id if update.effective_chat else "?",
            (user.full_name or user.username or str(user.id)) if user else "?",
            (update.effective_message.text or "")[:80] if update.effective_message else "(no msg)",
        )
        if self._test_disconnected:
            return
        msg = update.effective_message
        if not msg or not msg.text:
            return
        # mention_only mode: skip messages that don't @mention this bot,
        # and strip the bot @mention from the text before forwarding.
        if self._mention_only:
            bot_mention = f"@{context.bot.username}".lower()
            bot_entities = [
                e for e in (msg.entities or [])
                if e.type == "mention"
                and msg.text[e.offset : e.offset + e.length].lower() == bot_mention
            ]
            if not bot_entities:
                logger.debug(
                    "TG ignored (mention_only) [%s]: update_id=%s",
                    self._bot_username, update.update_id,
                )
                return
            # Remove bot @mention(s) from text using precise entity offsets
            text = msg.text
            for e in sorted(bot_entities, key=lambda x: x.offset, reverse=True):
                text = text[: e.offset] + text[e.offset + e.length :]
            text = text.strip()
        else:
            text = msg.text
        chat_id = str(msg.chat_id)
        user = msg.from_user
        user_id = str(user.id) if user else ""
        user_name = (
            (user.full_name or user.username or str(user.id)) if user else ""
        )
        chat_title = msg.chat.title or ""
        if self._on_message:
            await self._on_message(
                "telegram",
                self._bot_username,
                chat_id,
                user_id,
                user_name,
                chat_title,
                text,
                str(update.update_id),
            )

    async def _real_send_message(self, chat_id: str, text: str) -> bool:
        logger.info(
            "TG outbound [%s]: chat_id=%s len=%d text=%s",
            self._bot_username, chat_id, len(text), text[:80],
        )
        t0 = time.monotonic()
        try:
            await self._app.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.info("TG send ok [%s]: chat_id=%s elapsed=%.0fms", self._bot_username, chat_id, elapsed_ms)
            return True
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            # HTML parse failure (e.g. "<根因>" treated as tag) — fallback to plain text
            err_msg = str(e).lower()
            if "parse entities" in err_msg or "can't parse" in err_msg:
                logger.warning(
                    "TG HTML parse failed [%s]: %s elapsed=%.0fms, retrying plain text",
                    self._bot_username, e, elapsed_ms,
                )
                t1 = time.monotonic()
                try:
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                    )
                    logger.info("TG send ok (plain) [%s]: chat_id=%s elapsed=%.0fms", self._bot_username, chat_id, (time.monotonic() - t1) * 1000)
                    return True
                except Exception as e2:
                    logger.error("TG plain text send failed [%s]: %s", self._bot_username, e2, exc_info=True)
                    return False
            logger.error("TG send failed [%s]: chat_id=%s %s elapsed=%.0fms", self._bot_username, chat_id, e, elapsed_ms, exc_info=True)
            return False

    def _real_is_connected(self) -> bool:
        return self._connected
