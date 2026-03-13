import asyncio
import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)


class FeishuAdapter(ChannelAdapter):
    def __init__(self, app_id: str, app_secret: str, on_message: OnMessageCallback):
        super().__init__(name="feishu", on_message=on_message)
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )
        self._ws_client = None
        self._connected = False
        self._task = None
        self._loop = None  # store event loop reference for cross-thread callback

    async def start(self):
        self._loop = asyncio.get_event_loop()
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
        )
        self._task = asyncio.create_task(
            asyncio.to_thread(self._ws_client.start)
        )
        self._connected = True

    async def stop(self):
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _handle_message_event(self, event):
        """Feishu message callback (runs in thread).

        lark-oapi v1.5+ passes a single P2ImMessageReceiveV1 event object.
        """
        if self._test_disconnected:
            return
        try:
            msg = event.event.message
            sender = event.event.sender
            if msg.message_type != "text":
                return
            content = json.loads(msg.content).get("text", "")
            chat_id = msg.chat_id
            sender_id = (
                sender.sender_id.open_id
                if sender.sender_id
                else ""
            )
            if self._loop and self._on_message:
                asyncio.run_coroutine_threadsafe(
                    self._on_message(
                        "feishu",
                        self._app_id,
                        chat_id,
                        sender_id,
                        sender_id,
                        "",
                        content,
                        msg.message_id,
                    ),
                    self._loop,
                )
        except Exception as e:
            logger.error("Feishu event error: %s", e, exc_info=True)

    async def _real_send_message(self, group_id: str, text: str) -> bool:
        # Detect if text is Feishu post JSON (from formatter.to_feishu_post)
        is_post = False
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "zh_cn" in parsed:
                is_post = True
        except (json.JSONDecodeError, TypeError):
            pass

        if is_post:
            msg_type = "post"
            content = text  # Already JSON
        else:
            msg_type = "text"
            content = json.dumps({"text": text})

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(group_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = await asyncio.to_thread(
            self._client.im.v1.message.create, request
        )
        if not response.success():
            logger.error(
                "Feishu send failed: %s %s", response.code, response.msg
            )
            return False
        return True

    def _real_is_connected(self) -> bool:
        return self._connected
