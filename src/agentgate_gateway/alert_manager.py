"""Alert notification manager for AgentGate Gateway."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class AlertManager:
    def __init__(self, config):
        """config: AlertsConfig with telegram_bot_token, telegram_chat_id, feishu_webhook."""
        self._tg_sender = None
        self._tg_chat_id = ""
        self._feishu_webhook = getattr(config, "feishu_webhook", "") or ""

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

        # Send to Telegram
        if self._tg_sender:
            try:
                send_fn = getattr(self._tg_sender, "send_message", None)
                if send_fn is None:
                    send_fn = getattr(self._tg_sender, "send", None)
                if send_fn is None:
                    logger.error(
                        "TelegramSender has no send_message or send method, "
                        "Telegram alert not delivered"
                    )
                elif asyncio.iscoroutinefunction(send_fn):
                    await send_fn(self._tg_chat_id, text)
                else:
                    await asyncio.to_thread(send_fn, self._tg_chat_id, text)
            except Exception as e:
                logger.error("Telegram alert send failed: %s", e, exc_info=True)

        # Send to Feishu webhook (M-4)
        if self._feishu_webhook:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        self._feishu_webhook,
                        json={
                            "msg_type": "text",
                            "content": {"text": text},
                        },
                    )
                    if resp.status_code != 200:
                        logger.error(
                            "Feishu webhook failed: status=%d body=%s",
                            resp.status_code, resp.text[:200],
                        )
            except Exception as e:
                logger.error("Feishu webhook send failed: %s", e, exc_info=True)
