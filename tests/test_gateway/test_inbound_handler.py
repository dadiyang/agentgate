"""Tests for InboundHandler: dedup, routing, persist-before-process, retry, user notification."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agentgate_gateway.inbound_handler import InboundHandler, MAX_RETRY, RETRY_DELAYS


def _make_backend(url="http://backend:8000", token="tok"):
    b = MagicMock()
    b.url = url
    b.api_token = token
    return b


def _make_adapter():
    a = MagicMock()
    a.send_message = AsyncMock(return_value=True)
    return a


def _make_handler(db=None, router=None, backends=None, adapters=None):
    db = db or MagicMock()
    db.has_dedup_key = AsyncMock(return_value=False)
    db.save_inbound = AsyncMock()
    db.update_inbound_delivery = AsyncMock()
    db.increment_inbound_retry = AsyncMock()

    if router is None:
        router = MagicMock()
        router.match = MagicMock(return_value="backend-1")

    backends = backends if backends is not None else {"backend-1": _make_backend()}
    adapters = adapters if adapters is not None else {"telegram": _make_adapter()}

    handler = InboundHandler(db=db, router=router, backends=backends, adapters=adapters)
    return handler, db, router


MSG_DEFAULTS = dict(
    channel_type="telegram",
    bot_id="bot1",
    group_id="grp1",
    sender_id="usr1",
    sender_name="Alice",
    group_name="Test Group",
    text="Hello",
    dedup_key="dedup-abc",
)


@pytest.mark.asyncio
async def test_duplicate_message_ignored():
    """dedup_key already exists → save_inbound NOT called."""
    handler, db, _ = _make_handler()
    db.has_dedup_key = AsyncMock(return_value=True)

    await handler.handle_message(**MSG_DEFAULTS)

    db.save_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_no_route_silently_ignored():
    """Router returns None → save_inbound NOT called, no error raised."""
    router = MagicMock()
    router.match = MagicMock(return_value=None)
    handler, db, _ = _make_handler(router=router)

    await handler.handle_message(**MSG_DEFAULTS)

    db.save_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_successful_inject():
    """Mock httpx 200 {"ok": true} → delivery_status updated to 'delivered'."""
    handler, db, _ = _make_handler()

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json = MagicMock(return_value={"ok": True})

    with patch.object(handler._http, "post", new=AsyncMock(return_value=ok_response)):
        await handler.handle_message(**MSG_DEFAULTS)

    db.save_inbound.assert_called_once()
    db.update_inbound_delivery.assert_called_once()
    call_args = db.update_inbound_delivery.call_args
    assert call_args[0][1] == "delivered"


@pytest.mark.asyncio
async def test_inject_retry_on_failure():
    """Mock httpx to fail twice then succeed → retries work, final status 'delivered'."""
    handler, db, _ = _make_handler()

    fail_response = MagicMock()
    fail_response.status_code = 500
    fail_response.text = "Internal Server Error"
    fail_response.json = MagicMock(return_value={"ok": False})

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json = MagicMock(return_value={"ok": True})

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return fail_response
        return ok_response

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch.object(handler._http, "post", new=mock_post):
        await handler.handle_message(**MSG_DEFAULTS)

    assert call_count == 3
    db.update_inbound_delivery.assert_called_once()
    assert db.update_inbound_delivery.call_args[0][1] == "delivered"


@pytest.mark.asyncio
async def test_inject_all_retries_exhausted():
    """Mock httpx to always fail → delivery_status 'failed', user notified (AC-29)."""
    adapter = _make_adapter()
    handler, db, _ = _make_handler(adapters={"telegram": adapter})

    fail_response = MagicMock()
    fail_response.status_code = 500
    fail_response.text = "Error"
    fail_response.json = MagicMock(return_value={"ok": False})

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch.object(handler._http, "post", new=AsyncMock(return_value=fail_response)):
        await handler.handle_message(**MSG_DEFAULTS)

    # delivery status must be set to 'failed'
    db.update_inbound_delivery.assert_called_once()
    assert db.update_inbound_delivery.call_args[0][1] == "failed"

    # user must be notified in the IM group (AC-29)
    adapter.send_message.assert_called_once()
    notify_args = adapter.send_message.call_args[0]
    assert notify_args[0] == MSG_DEFAULTS["group_id"]
    assert "⚠️" in notify_args[1]


@pytest.mark.asyncio
async def test_persist_before_inject():
    """Verify save_inbound is called BEFORE httpx.post (ordering guarantee)."""
    handler, db, _ = _make_handler()

    call_order = []

    async def mock_save_inbound(msg):
        call_order.append("save_inbound")

    async def mock_post(*args, **kwargs):
        call_order.append("http_post")
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"ok": True})
        return resp

    db.save_inbound = mock_save_inbound

    with patch.object(handler._http, "post", new=mock_post):
        await handler.handle_message(**MSG_DEFAULTS)

    assert call_order.index("save_inbound") < call_order.index("http_post"), \
        "save_inbound must be called before http_post"


@pytest.mark.asyncio
async def test_retry_delays_applied():
    """Verify asyncio.sleep is called with correct delays between retries."""
    handler, db, _ = _make_handler()

    fail_response = MagicMock()
    fail_response.status_code = 500
    fail_response.text = "Error"
    fail_response.json = MagicMock(return_value={"ok": False})

    sleep_calls = []

    async def mock_sleep(delay):
        sleep_calls.append(delay)

    with patch("asyncio.sleep", new=mock_sleep), \
         patch.object(handler._http, "post", new=AsyncMock(return_value=fail_response)):
        await handler.handle_message(**MSG_DEFAULTS)

    # RETRY_DELAYS has entries for attempts 0 and 1 (before attempts 1 and 2)
    # After the last attempt (2) there's no sleep
    assert sleep_calls == RETRY_DELAYS[:MAX_RETRY - 1]


@pytest.mark.asyncio
async def test_backend_not_configured():
    """Backend missing from dict → status 'failed', no crash."""
    handler, db, _ = _make_handler(backends={})  # empty backends

    await handler.handle_message(**MSG_DEFAULTS)

    db.update_inbound_delivery.assert_called_once()
    assert db.update_inbound_delivery.call_args[0][1] == "failed"
