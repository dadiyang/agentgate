import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Callback signature for inbound messages
OnMessageCallback = Callable[
    [str, str, str, str, str, str, str, str],
    # channel_type, bot_id, chat_id, sender_id, sender_name, group_name, text, dedup_key
    Awaitable[None],
]


class ChannelAdapter(ABC):
    def __init__(self, name: str, on_message: OnMessageCallback | None):
        self.name = name
        self._on_message = on_message
        self._test_disconnected = False  # Admin API test mode

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def _real_send_message(self, chat_id: str, text: str) -> bool: ...

    @abstractmethod
    def _real_is_connected(self) -> bool: ...

    async def send_message(self, chat_id: str, text: str) -> bool:
        if self._test_disconnected:
            logger.warning(
                "Adapter %s: test_disconnected, simulating send failure", self.name
            )
            return False
        return await self._real_send_message(chat_id, text)

    def is_connected(self) -> bool:
        if self._test_disconnected:
            return False
        return self._real_is_connected()

    def test_disconnect(self, duration: int = 0):
        self._test_disconnected = True
        if duration > 0:
            loop = asyncio.get_event_loop()
            loop.call_later(duration, self.test_reconnect)

    def test_reconnect(self):
        self._test_disconnected = False
