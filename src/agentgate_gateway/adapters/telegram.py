import asyncio
import logging
import os

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
    ):
        super().__init__(name="telegram", on_message=on_message)
        self._bot_token = bot_token
        self._bot_id_override = bot_id_override
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
        logger.info(
            "TG inbound [%s]: update_id=%s chat_id=%s text=%s",
            self._bot_username,
            update.update_id,
            update.effective_chat.id if update.effective_chat else "?",
            (update.effective_message.text or "")[:50] if update.effective_message else "(no msg)",
        )
        if self._test_disconnected:
            return
        msg = update.effective_message
        if not msg or not msg.text:
            return
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
                msg.text,
                str(update.update_id),
            )

    async def _real_send_message(self, chat_id: str, text: str) -> bool:
        logger.info(
            "TG outbound [%s]: chat_id=%s len=%d text=%s",
            self._bot_username, chat_id, len(text), text[:80],
        )
        try:
            await self._app.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
            )
            return True
        except Exception as e:
            # HTML parse failure (e.g. "<根因>" treated as tag) — fallback to plain text
            err_msg = str(e).lower()
            if "parse entities" in err_msg or "can't parse" in err_msg:
                logger.warning(
                    "Telegram HTML parse failed, retrying as plain text: %s", e,
                )
                try:
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=text,
                    )
                    return True
                except Exception as e2:
                    logger.error("Telegram plain text send also failed: %s", e2, exc_info=True)
                    return False
            logger.error("Telegram send failed: %s", e, exc_info=True)
            return False

    def _real_is_connected(self) -> bool:
        return self._connected
