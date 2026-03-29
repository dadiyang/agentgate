import asyncio
import json
import logging
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)

# How long to wait after thread start to check if WS connected successfully
_STARTUP_CHECK_DELAY = 3.0
# How often to poll thread liveness when start() blocks
_LIVENESS_POLL_INTERVAL = 5.0
# Force reconnect after this age regardless of connection health.
# Mitigates server-side bug where connection is alive (pongs work) but
# messages are not delivered.  websockets' built-in keepalive (20s ping/pong)
# already handles truly dead connections, so we only need this for the
# "alive but not delivering" case.
_MAX_CONNECTION_AGE = 1800  # 30 minutes
# Drop inbound messages older than this (seconds).  After periodic reconnect,
# Feishu may re-deliver old messages with new msg_ids that bypass dedup.
_MAX_MESSAGE_AGE = 3600  # 1 hour
# Counter for staggering multi-app startup to avoid module-level loop race.
_feishu_instance_count = 0
_feishu_count_lock = threading.Lock()


class FeishuAdapter(ChannelAdapter):
    def __init__(self, app_id: str, app_secret: str, on_message: OnMessageCallback):
        super().__init__(name="feishu", on_message=on_message)
        self._app_id = app_id
        # Assign startup delay to stagger WS init across instances
        with _feishu_count_lock:
            global _feishu_instance_count
            self._startup_delay = _feishu_instance_count * 5.0
            _feishu_instance_count += 1
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
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._connected_at: float = 0.0

    async def start(self):
        """Start Feishu WebSocket in a dedicated thread with its own event loop.

        lark-oapi ws.Client.start() uses a module-level ``loop`` global. Multiple
        adapters must not race on this global, so they are staggered by 5s each.
        """
        if self._startup_delay > 0:
            logger.info("Feishu app %s: waiting %.0fs for staggered startup", self._app_id, self._startup_delay)
            await asyncio.sleep(self._startup_delay)
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
        self._connected_at = time.monotonic()
        logger.info("Feishu WebSocket connected [%s]", self._app_id)

        # Block until thread exits (adapter_run_loop expects start() to block)
        while self._ws_thread.is_alive():
            await asyncio.sleep(_LIVENESS_POLL_INTERVAL)

            # Periodic reconnect: mitigates server-side bug where connection is
            # alive (pongs work) but messages stop being delivered.
            # Dead connections are already handled by websockets' built-in
            # keepalive (ping_interval=20s, ping_timeout=20s).
            age = time.monotonic() - self._connected_at
            if age > _MAX_CONNECTION_AGE:
                logger.info(
                    "Feishu [%s]: connection age %.0fs exceeds %ds, periodic reconnect",
                    self._app_id, age, _MAX_CONNECTION_AGE,
                )
                self._force_close_ws()
                self._connected_at = time.monotonic()
                continue

        # Thread exited = connection lost
        self._connected = False
        logger.warning("Feishu WebSocket disconnected [%s]", self._app_id)
        err = self._ws_error or ConnectionError("Feishu WebSocket connection lost")
        raise ConnectionError(f"Feishu WebSocket disconnected: {err}")

    def _run_ws_blocking(self):
        """Run ws.Client.start() in a dedicated thread with its own event loop.

        lark SDK uses a module-level ``loop`` global for create_task() in _connect
        and _receive_message_loop. With multiple adapters, they overwrite each
        other's loop causing "Future attached to a different loop" errors.

        Fix: monkey-patch _connect and _receive_message_loop on each Client
        instance to capture our thread-local loop in a closure, bypassing the
        module-level global entirely.
        """
        import lark_oapi.ws.client as ws_client_mod

        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        # Set module-level loop for start()/run_until_complete calls
        ws_client_mod.loop = new_loop
        self._ws_loop = new_loop

        # Monkey-patch _connect and _receive_message_loop to use the captured
        # new_loop instead of the module-level loop (overwritten by other adapters).
        client = self._ws_client
        _lark_logger = ws_client_mod.logger

        async def _patched_connect(self_client):
            """_connect with instance-local loop for create_task.

            Also fixes a lock leak in the original SDK: the early return when
            _conn is not None must release the lock (moved into try/finally).
            """
            await self_client._lock.acquire()
            try:
                if self_client._conn is not None:
                    return
                import websockets
                conn_url = self_client._get_conn_url()
                from urllib.parse import urlparse, parse_qs
                u = urlparse(conn_url)
                q = parse_qs(u.query)
                conn_id = q["device_id"][0]
                service_id = q["service_id"][0]
                conn = await websockets.connect(conn_url)
                self_client._conn = conn
                self_client._conn_url = conn_url
                self_client._conn_id = conn_id
                self_client._service_id = service_id
                self._connected_at = time.monotonic()
                logger.info("Feishu WS connected [%s]: conn_id=%s", self._app_id, conn_id)
                new_loop.create_task(self_client._receive_message_loop())
            except Exception as e:
                _lark_logger.error(self_client._fmt_log("connect failed: {}", e))
                raise
            finally:
                self_client._lock.release()

        async def _patched_recv_loop(self_client):
            """_receive_message_loop with instance-local loop for create_task."""
            try:
                while True:
                    if self_client._conn is None:
                        raise Exception("connection is closed")
                    msg = await self_client._conn.recv()
                    new_loop.create_task(self_client._handle_message(msg))
            except Exception as e:
                _lark_logger.error(self_client._fmt_log("receive message loop exit, err: {}", e))
                try:
                    await self_client._disconnect()
                except Exception as de:
                    _lark_logger.error(self_client._fmt_log("disconnect error: {}", de))
                if self_client._auto_reconnect:
                    logger.info("Feishu [%s]: auto-reconnecting after receive loop exit", self._app_id)
                    await self_client._reconnect()
                else:
                    raise

        import types
        client._connect = types.MethodType(_patched_connect, client)
        client._receive_message_loop = types.MethodType(_patched_recv_loop, client)

        try:
            self._ws_client.start()
        except Exception as e:
            logger.error("Feishu WS thread error: %s", e, exc_info=True)
            self._ws_error = e
            self._connected = False
        finally:
            try:
                new_loop.close()
            except Exception as e:
                logger.warning("Feishu [%s]: failed to close WS event loop: %s", self._app_id, e)
                pass

    def _force_close_ws(self):
        """Force close WS connection to trigger recv loop exit → auto reconnect.

        Schedules an async close on the WS thread's event loop.  When the
        underlying connection closes, recv() raises ConnectionClosed, which the
        patched recv loop catches and feeds into the SDK's reconnect flow.
        """
        ws_loop = self._ws_loop
        conn = getattr(self._ws_client, "_conn", None) if self._ws_client else None
        if ws_loop and not ws_loop.is_closed() and conn:
            async def _close():
                try:
                    await conn.close()
                except Exception as e:
                    logger.warning("Feishu [%s]: error during forced WS close: %s", self._app_id, e)
                    pass
            asyncio.run_coroutine_threadsafe(_close(), ws_loop)

    async def stop(self):
        self._connected = False
        if self._ws_client:
            try:
                ws_loop = self._ws_loop
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
        try:
            msg = event.event.message
            sender = event.event.sender
            # Raw event log — first line before any filtering
            logger.info(
                "Feishu raw event [%s]: msg_id=%s chat_id=%s sender_type=%s msg_type=%s content=%s",
                self._app_id, msg.message_id, msg.chat_id,
                sender.sender_type, msg.message_type,
                (msg.content or "")[:80],
            )
            # Drop stale messages re-delivered after reconnect (new msg_id bypasses dedup)
            if msg.create_time:
                try:
                    msg_age = time.time() - int(msg.create_time) / 1000
                    if msg_age > _MAX_MESSAGE_AGE:
                        logger.warning(
                            "Feishu [%s]: dropping stale message (age=%.0fs): msg_id=%s content=%s",
                            self._app_id, msg_age, msg.message_id, (msg.content or "")[:60],
                        )
                        return
                except (ValueError, TypeError):
                    pass  # Can't parse create_time, let it through
            if self._test_disconnected:
                logger.debug("Feishu [%s]: test_disconnected, dropping", self._app_id)
                return
            # Ignore non-user messages to prevent bot-to-bot loops in multi-app groups
            if sender.sender_type != "user":
                logger.debug("Feishu [%s]: ignoring non-user sender_type=%s", self._app_id, sender.sender_type)
                return
            if msg.message_type != "text":
                logger.debug("Feishu [%s]: ignoring msg_type=%s", self._app_id, msg.message_type)
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
            logger.error("Feishu event callback error [%s]: %s", self._app_id, e, exc_info=True)

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
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Feishu [%s]: failed to parse message as post JSON: %s", self._app_id, e)
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
        t0 = time.monotonic()
        response = await asyncio.to_thread(
            self._client.im.v1.message.create, request
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        if not response.success():
            logger.error(
                "Feishu send failed [%s]: chat_id=%s code=%s msg=%s elapsed=%.0fms",
                self._app_id, chat_id, response.code, response.msg, elapsed_ms,
            )
            return False
        logger.info("Feishu send ok [%s]: chat_id=%s elapsed=%.0fms", self._app_id, chat_id, elapsed_ms)
        return True

    def _real_is_connected(self) -> bool:
        return self._connected
