"""Tests for OutputPoller: filtering, persistence, push failures, offset tracking."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentgate_gateway.output_poller import OutputPoller


def _make_backend(url="http://backend:8000", token="tok", status="healthy"):
    b = MagicMock()
    b.url = url
    b.api_token = token
    b.status = status
    return b


def _make_adapter(send_result=True):
    a = MagicMock()
    a.send_message = AsyncMock(return_value=send_result)
    return a


def _make_db():
    db = MagicMock()
    db.save_outbound = AsyncMock()
    db.update_status = AsyncMock()
    db.has_content_hash = AsyncMock(return_value=False)
    db.increment_retry = AsyncMock()
    db.save_poll_offset = AsyncMock()
    db.load_poll_offsets = AsyncMock(return_value={})
    return db


def _make_router(bindings=None):
    router = MagicMock()
    # reverse_lookup returns list of (channel_type, bot_id, chat_id)
    router.reverse_lookup = MagicMock(
        return_value=bindings or [("telegram", "bot1", "grp1")]
    )
    return router


def _make_output_response(messages, next_offset=10):
    return {
        "ok": True,
        "count": len(messages),
        "messages": messages,
        "next_offset": next_offset,
    }


def _mock_http_get(poller, response_data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=response_data)
    poller._http.get = AsyncMock(return_value=resp)


async def _drain_push_tasks(poller):
    """Await all background push tasks created during _poll_backend."""
    if poller._push_tasks:
        await asyncio.gather(*list(poller._push_tasks))


@pytest.mark.asyncio
async def test_thinking_messages_filtered():
    """Backend returns thinking + text messages → only text pushed to channel."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    poller._offsets["b1"] = 0  # pre-seed so seeding phase is skipped
    messages = [
        {"content_type": "thinking", "text": "Let me think..."},
        {"content_type": "text", "text": "Hello from agent"},
    ]
    _mock_http_get(poller, _make_output_response(messages))

    await poller._poll_backend("b1", backend)
    await _drain_push_tasks(poller)

    adapter.send_message.assert_called_once()
    sent_text = adapter.send_message.call_args[0][1]
    assert "Hello from agent" in sent_text
    assert "Let me think..." not in sent_text


@pytest.mark.asyncio
async def test_all_thinking_no_push():
    """All messages are thinking blocks → nothing pushed."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    poller._offsets["b1"] = 0
    messages = [
        {"content_type": "thinking", "text": "step 1"},
        {"content_type": "thinking", "text": "step 2"},
    ]
    _mock_http_get(poller, _make_output_response(messages))

    await poller._poll_backend("b1", backend)
    await _drain_push_tasks(poller)

    adapter.send_message.assert_not_called()
    db.save_outbound.assert_not_called()


@pytest.mark.asyncio
async def test_output_persisted_before_push():
    """Verify save_outbound is called BEFORE send_message (crash safety)."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()

    call_order = []

    async def mock_save_outbound(msg):
        call_order.append("save_outbound")

    async def mock_send_message(chat_id, text):
        call_order.append("send_message")
        return True

    adapter = MagicMock()
    adapter.send_message = mock_send_message

    db.save_outbound = mock_save_outbound

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    poller._offsets["b1"] = 0
    messages = [{"content_type": "text", "text": "Hello"}]
    _mock_http_get(poller, _make_output_response(messages))

    await poller._poll_backend("b1", backend)
    await _drain_push_tasks(poller)

    assert call_order.index("save_outbound") < call_order.index("send_message"), \
        "save_outbound must be called before send_message"


@pytest.mark.asyncio
async def test_push_failure_marks_failed():
    """adapter.send_message returns False → status updated to 'failed'."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()
    adapter = _make_adapter(send_result=False)

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    poller._offsets["b1"] = 0
    messages = [{"content_type": "text", "text": "Hello"}]
    _mock_http_get(poller, _make_output_response(messages))

    with patch("asyncio.sleep", new=AsyncMock()):
        await poller._poll_backend("b1", backend)
        await _drain_push_tasks(poller)

    db.update_status.assert_called()
    final_status_call = db.update_status.call_args_list[-1]
    assert final_status_call[0][1] == "failed"


@pytest.mark.asyncio
async def test_unhealthy_backend_skipped():
    """backend.status = 'unhealthy' → _poll_backend not called."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend(status="unhealthy")
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    # Override poll_backend to track if it's called
    poller._poll_backend = AsyncMock()

    # Patch asyncio.sleep to stop the loop after one iteration
    async def mock_sleep(delay):
        poller.stop()

    with patch("asyncio.sleep", new=mock_sleep):
        await poller.run()

    poller._poll_backend.assert_not_called()


@pytest.mark.asyncio
async def test_offset_tracking():
    """After polling, offset is updated to next_offset from backend response."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )

    poller._offsets["b1"] = 0  # pre-seed, simulating a previously-seen backend
    assert poller._offsets["b1"] == 0

    messages = [{"content_type": "text", "text": "Hello"}]
    _mock_http_get(poller, _make_output_response(messages, next_offset=42))

    await poller._poll_backend("b1", backend)

    assert poller._offsets["b1"] == 42


@pytest.mark.asyncio
async def test_offset_not_updated_on_empty_response():
    """Empty response (count=0) → offset stays unchanged."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend()
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
    )
    poller._offsets["b1"] = 10

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json = MagicMock(return_value={"ok": True, "count": 0, "messages": []})
    poller._http.get = AsyncMock(return_value=empty_resp)

    await poller._poll_backend("b1", backend)

    assert poller._offsets["b1"] == 10


@pytest.mark.asyncio
async def test_no_adapter_marks_failed():
    """No adapter registered for channel → outbound marked failed."""
    db = _make_db()
    router = _make_router(bindings=[("wechat", "bot1", "grp1")])
    backend = _make_backend()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={},  # no adapter for 'wechat'
    )

    poller._offsets["b1"] = 0
    messages = [{"content_type": "text", "text": "Hello"}]
    _mock_http_get(poller, _make_output_response(messages))

    await poller._poll_backend("b1", backend)
    await _drain_push_tasks(poller)

    db.save_outbound.assert_called_once()
    db.update_status.assert_called()
    assert db.update_status.call_args[0][1] == "failed"


@pytest.mark.asyncio
async def test_repush_message_success():
    """repush_message pushes and updates status to 'delivered'."""
    db = _make_db()
    adapter = _make_adapter(send_result=True)

    poller = OutputPoller(
        db=db, router=_make_router(),
        backends={},
        adapters={"telegram": adapter},
    )

    msg = {
        "id": "msg-123",
        "channel_type": "telegram",
        "chat_id": "grp1",
        "content": "Hello",
    }
    await poller.repush_message(msg)

    adapter.send_message.assert_called_once_with("grp1", "Hello")
    db.update_status.assert_called_once()
    assert db.update_status.call_args[0][1] == "delivered"


@pytest.mark.asyncio
async def test_run_loop_polls_healthy_backends():
    """run() iterates over healthy backends and calls _poll_backend."""
    db = _make_db()
    router = _make_router()
    backend = _make_backend(status="healthy")
    adapter = _make_adapter()

    poller = OutputPoller(
        db=db, router=router,
        backends={"b1": backend},
        adapters={"telegram": adapter},
        poll_interval=0.1,
    )

    poll_calls = []

    async def mock_poll_backend(backend_id, backend):
        poll_calls.append(backend_id)

    poller._poll_backend = mock_poll_backend

    async def mock_sleep(delay):
        poller.stop()

    with patch("asyncio.sleep", new=mock_sleep):
        await poller.run()

    assert "b1" in poll_calls
