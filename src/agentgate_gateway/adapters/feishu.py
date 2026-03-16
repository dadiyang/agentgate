import asyncio
import json
import logging
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)

# How long to wait after thread start to check if WS connected successfully
_STARTUP_CHECK_DELAY = 3.0
# How often to poll thread liveness when start() blocks
_LIVENESS_POLL_INTERVAL = 5.0
# Serialize ws_client_mod.loop monkey-patch across multiple FeishuAdapter instances.
# Each adapter's _run_ws_blocking sets the module-level loop, then calls start()
# which captures it internally. The lock ensures no two adapters race on this global.
_WS_INIT_LOCK = threading.Lock()


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
        self._ws_thread: threading.Thread | None = None
        self._ws_error: Exception | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self):
        """Start Feishu WebSocket in a dedicated thread with its own event loop.

        lark-oapi ws.Client.start() is synchronous and internally calls
        loop.run_until_complete() on a module-level event loop. Running it
        in the main asyncio thread (even via to_thread) fails because the
        module-level loop reference points to the already-running main loop.

        Fix: spawn a dedicated thread, create a NEW event loop there, and
        monkey-patch the module-level ``loop`` before calling start().
        """
        self._loop = asyncio.get_event_loop()
        self._ws_error = None

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

        self._ws_thread = threading.Thread(
            target=self._run_ws_blocking, daemon=True, name="feishu-ws"
        )
        self._ws_thread.start()

        # Wait for initial connection attempt
        await asyncio.sleep(_STARTUP_CHECK_DELAY)

        if not self._ws_thread.is_alive():
            self._connected = False
            err = self._ws_error or RuntimeError("Feishu WebSocket thread died on startup")
            raise ConnectionError(f"Feishu WebSocket startup failed: {err}")

        self._connected = True
        logger.info("Feishu WebSocket connected")

        # Block until thread exits (adapter_run_loop expects start() to block)
        while self._ws_thread.is_alive():
            await asyncio.sleep(_LIVENESS_POLL_INTERVAL)

        # Thread exited = connection lost
        self._connected = False
        err = self._ws_error or ConnectionError("Feishu WebSocket connection lost")
        raise ConnectionError(f"Feishu WebSocket disconnected: {err}")

    def _run_ws_blocking(self):
        """Run ws.Client.start() in a dedicated thread with its own event loop."""
        import lark_oapi.ws.client as ws_client_mod

        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        # Monkey-patch the Client instance's start method to use our thread-local
        # loop instead of the module-level global. This completely avoids the race
        # condition — each adapter has its own loop and its own start method.
        _original_connect = self._ws_client._connect
        _original_disconnect = self._ws_client._disconnect
        _original_reconnect = self._ws_client._reconnect
        _original_ping = self._ws_client._ping_loop
        _auto_reconnect = self._ws_client._auto_reconnect

        async def _select_forever():
            """Block forever (replacement for module-level _select)."""
            await asyncio.get_event_loop().create_future()

        def _patched_start():
            try:
                new_loop.run_until_complete(_original_connect())
            except Exception as e:
                logger.error("Feishu WS connect failed: %s", e, exc_info=True)
                new_loop.run_until_complete(_original_disconnect())
                if _auto_reconnect:
                    new_loop.run_until_complete(_original_reconnect())
                else:
                    raise
            new_loop.create_task(_original_ping())
            new_loop.run_until_complete(_select_forever())

        self._ws_client.start = _patched_start
        try:
            self._ws_client.start()
        except Exception as e:
            logger.error("Feishu WS thread error: %s", e, exc_info=True)
            self._ws_error = e
            self._connected = False
        finally:
            try:
                new_loop.close()
            except Exception:
                pass

    async def stop(self):
        self._connected = False
        # The WS thread is a daemon thread; closing the internal connection
        # will cause start() to exit, or the thread will be killed at process exit.
        if self._ws_client:
            try:
                import lark_oapi.ws.client as ws_client_mod
                ws_loop = ws_client_mod.loop
                conn = getattr(self._ws_client, "_conn", None)
                if conn and ws_loop and not ws_loop.is_closed():
                    future = asyncio.run_coroutine_threadsafe(conn.close(), ws_loop)
                    future.result(timeout=5)
            except Exception as e:
                logger.debug("Feishu WS stop cleanup: %s", e)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)

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

    async def _real_send_message(self, chat_id: str, text: str) -> bool:
        logger.info(
            "Feishu outbound: chat_id=%s len=%d text=%s",
            chat_id, len(text), text[:80],
        )
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
                .receive_id(chat_id)
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
