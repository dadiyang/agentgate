"""Tests for ChannelAdapter base class using a MockAdapter."""

import asyncio
import pytest

from agentgate_gateway.adapters.base import ChannelAdapter, OnMessageCallback


class MockAdapter(ChannelAdapter):
    """Concrete adapter for testing the base class logic."""

    def __init__(self, on_message=None):
        super().__init__(name="mock", on_message=on_message)
        self._running = False
        self._send_calls: list[tuple[str, str]] = []
        self._send_result = True

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def _real_send_message(self, group_id: str, text: str) -> bool:
        self._send_calls.append((group_id, text))
        return self._send_result

    def _real_is_connected(self) -> bool:
        return self._running


class TestChannelAdapterBase:
    """Test ChannelAdapter base class behavior."""

    @pytest.fixture
    def adapter(self):
        return MockAdapter()

    async def test_normal_send_works(self, adapter):
        """Normal send_message delegates to _real_send_message."""
        await adapter.start()
        result = await adapter.send_message("group_1", "hello")
        assert result is True
        assert adapter._send_calls == [("group_1", "hello")]

    async def test_is_connected_reflects_real_state(self, adapter):
        """is_connected() delegates to _real_is_connected() when not test_disconnected."""
        assert adapter.is_connected() is False
        await adapter.start()
        assert adapter.is_connected() is True
        await adapter.stop()
        assert adapter.is_connected() is False

    async def test_test_disconnect_blocks_send(self, adapter):
        """test_disconnect causes send_message to return False without calling real send."""
        await adapter.start()
        adapter.test_disconnect()
        result = await adapter.send_message("group_1", "hello")
        assert result is False
        # _real_send_message should NOT have been called
        assert adapter._send_calls == []

    async def test_test_disconnect_makes_is_connected_false(self, adapter):
        """test_disconnect makes is_connected() return False even if adapter is running."""
        await adapter.start()
        assert adapter.is_connected() is True
        adapter.test_disconnect()
        assert adapter.is_connected() is False

    async def test_test_reconnect_restores_normal_behavior(self, adapter):
        """test_reconnect reverses the effect of test_disconnect."""
        await adapter.start()
        adapter.test_disconnect()
        assert adapter.is_connected() is False

        adapter.test_reconnect()
        assert adapter.is_connected() is True

        result = await adapter.send_message("group_1", "hello after reconnect")
        assert result is True
        assert len(adapter._send_calls) == 1

    async def test_test_disconnect_flag_is_set(self, adapter):
        """test_disconnect sets _test_disconnected flag."""
        assert adapter._test_disconnected is False
        adapter.test_disconnect()
        assert adapter._test_disconnected is True

    async def test_test_reconnect_clears_flag(self, adapter):
        """test_reconnect clears _test_disconnected flag."""
        adapter.test_disconnect()
        assert adapter._test_disconnected is True
        adapter.test_reconnect()
        assert adapter._test_disconnected is False

    async def test_adapter_name_stored(self, adapter):
        """Adapter stores its name."""
        assert adapter.name == "mock"

    async def test_send_message_result_propagated(self, adapter):
        """send_message propagates the return value from _real_send_message."""
        await adapter.start()
        adapter._send_result = False
        result = await adapter.send_message("group_1", "hello")
        assert result is False

    async def test_multiple_messages_accumulate(self, adapter):
        """Multiple send_message calls all go through to real send."""
        await adapter.start()
        await adapter.send_message("g1", "msg1")
        await adapter.send_message("g2", "msg2")
        await adapter.send_message("g1", "msg3")
        assert len(adapter._send_calls) == 3
        assert adapter._send_calls[0] == ("g1", "msg1")
        assert adapter._send_calls[2] == ("g1", "msg3")

    async def test_test_disconnect_with_duration_sets_flag(self, adapter):
        """test_disconnect with duration sets the flag (duration scheduling tested via flag)."""
        await adapter.start()
        # Just verify the flag is set — the actual timer is loop.call_later
        adapter.test_disconnect(duration=100)
        assert adapter._test_disconnected is True

    async def test_on_message_callback_stored(self):
        """on_message callback is stored and accessible."""

        async def my_callback(*args):
            pass

        adapter = MockAdapter(on_message=my_callback)
        assert adapter._on_message is my_callback

    async def test_no_callback_no_error(self, adapter):
        """Adapter without callback can still send without error."""
        await adapter.start()
        result = await adapter.send_message("g1", "test")
        assert result is True
