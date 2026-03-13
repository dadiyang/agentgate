"""Alert notification manager for AgentGate Gateway."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class AlertManager:
    def __init__(self, config):
        """config: AlertsConfig with telegram_bot_token, telegram_chat_id, feishu_webhook."""
        self._tg_sender = None
        self._tg_chat_id = ""
        tg_token = getattr(config, "telegram_bot_token", "") or ""
        tg_chat = getattr(config, "telegram_chat_id", "") or ""
        if tg_token and tg_chat:
            try:
                from haloant_kit.telegram import TelegramSender

                self._tg_sender = TelegramSender(tg_token)
                self._tg_chat_id = tg_chat
            except ImportError:
                logger.warning(
                    "haloant_kit.telegram not available, Telegram alerts disabled"
                )

    async def send(
        self, alert_type: str, severity: str, detail: str, affected: str = ""
    ):
        text = (
            f"🚨 [AgentGate 告警]\n"
            f"类型：{alert_type}\n"
            f"严重度：{severity}\n"
            f"影响：{affected}\n"
            f"详情：{detail}"
        )
        logger.warning(
            "ALERT [%s] %s: %s (affected: %s)", severity, alert_type, detail, affected
        )
        if self._tg_sender:
            try:
                # TelegramSender.send_message is async; fall back to asyncio.to_thread
                # for sync variants if needed
                send_fn = getattr(self._tg_sender, "send_message", None)
                if send_fn is None:
                    # Older API: try generic 'send'
                    send_fn = getattr(self._tg_sender, "send", None)
                if send_fn is None:
                    logger.error(
                        "TelegramSender has no send_message or send method, "
                        "Telegram alert not delivered"
                    )
                    return

                if asyncio.iscoroutinefunction(send_fn):
                    await send_fn(self._tg_chat_id, text)
                else:
                    await asyncio.to_thread(send_fn, self._tg_chat_id, text)
            except Exception as e:
                logger.error("Alert send failed: %s", e, exc_info=True)
