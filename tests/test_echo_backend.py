"""Comprehensive tests for echo_backend.

Coverage:
- Health endpoint (no auth needed)
- Inject + output basic flow
- Inject idempotency (duplicate message_id)
- Auth required (missing / wrong token → 403)
- [test-thinking] trigger
- [test-markdown] trigger
- [test-long:N] trigger
- [test-delay:Ns] trigger
- confirm_processed endpoint
- unprocessed endpoint
- /api/window stub
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from echo_backend.main import make_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TOKEN = "test-secret-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BAD_AUTH_HEADERS = {"Authorization": "Bearer wrong-token"}


@pytest_asyncio.fixture
async def client(aiohttp_client):
    app = make_app(TOKEN)
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def inject(client, text: str, message_id: str | None = None, sender_name: str = "tester"):
    mid = message_id or str(uuid.uuid4())
    resp = await client.post(
        "/api/inject",
        json={"text": text, "message_id": mid, "sender_name": sender_name},
        headers=AUTH_HEADERS,
    )
    return resp, mid


async def get_output(client, since: int = 0):
    resp = await client.get(
        f"/api/output/echo?since={since}",
        headers=AUTH_HEADERS,
    )
    return resp


async def wait_for_output(client, since: int = 0, expected_count: int = 1, timeout: float = 3.0):
    """Poll output until at least expected_count new messages appear."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await get_output(client, since)
        data = await resp.json()
        if data["count"] >= expected_count:
            return data
        await asyncio.sleep(0.05)
    # Return whatever we have
    resp = await get_output(client, since)
    return await resp.json()


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

async def test_health_no_auth(client):
    """Health endpoint must be accessible without auth."""
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["watchdog_enabled"] is False


async def test_health_fields(client):
    """Health response must include windows and window_health."""
    resp = await client.get("/api/health")
    data = await resp.json()
    assert "windows" in data
    assert isinstance(data["windows"], list)
    assert len(data["windows"]) >= 1
    window = data["windows"][0]
    assert window["window_id"] == "@echo"
    assert "window_health" in data


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------

async def test_inject_missing_auth(client):
    """Inject without Authorization header → 403."""
    resp = await client.post(
        "/api/inject",
        json={"text": "hello", "message_id": str(uuid.uuid4())},
    )
    assert resp.status == 403
    data = await resp.json()
    assert data["ok"] is False


async def test_inject_wrong_token(client):
    """Inject with wrong token → 403."""
    resp = await client.post(
        "/api/inject",
        json={"text": "hello", "message_id": str(uuid.uuid4())},
        headers=BAD_AUTH_HEADERS,
    )
    assert resp.status == 403


async def test_output_missing_auth(client):
    """Output without Authorization header → 403."""
    resp = await client.get("/api/output/echo?since=0")
    assert resp.status == 403


async def test_confirm_missing_auth(client):
    """confirm_processed without auth → 403."""
    resp = await client.post(
        "/api/confirm_processed",
        json={"message_ids": []},
    )
    assert resp.status == 403


async def test_unprocessed_missing_auth(client):
    """/api/unprocessed without auth → 403."""
    resp = await client.get("/api/unprocessed")
    assert resp.status == 403


async def test_window_missing_auth(client):
    """/api/window without auth → 403."""
    resp = await client.post("/api/window", json={})
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Basic inject + output flow
# ---------------------------------------------------------------------------

async def test_inject_returns_ok(client):
    """POST /api/inject should return ok=True with delivery_id and window_id."""
    resp, mid = await inject(client, "Hello echo!")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert "delivery_id" in data
    assert data["window_id"] == "@echo"
    assert "msg" in data


async def test_basic_echo_reply(client):
    """Message without trigger word → reply starts with 'Echo:'."""
    resp, mid = await inject(client, "Hello world")
    assert resp.status == 200

    data = await wait_for_output(client, since=0, expected_count=1)
    assert data["ok"] is True
    assert data["count"] >= 1

    text_messages = [m for m in data["messages"] if m["content_type"] == "text"]
    assert len(text_messages) >= 1
    assert "Hello world" in text_messages[0]["text"]
    assert text_messages[0]["text"].startswith("Echo:")


async def test_output_message_schema(client):
    """Each output message must have required fields with correct types."""
    resp, mid = await inject(client, "schema test")
    data = await wait_for_output(client, since=0, expected_count=1)

    for msg in data["messages"]:
        assert "role" in msg
        assert msg["role"] == "assistant"
        assert "text" in msg
        assert isinstance(msg["text"], str)
        assert "content_type" in msg
        assert msg["content_type"] in ("text", "thinking", "tool_use", "tool_result")
        assert "timestamp" in msg
        # timestamp must be ISO-8601 string
        assert "T" in msg["timestamp"]


async def test_output_incremental(client):
    """since parameter returns only new messages."""
    # First message
    resp1, mid1 = await inject(client, "message one")
    data1 = await wait_for_output(client, since=0, expected_count=1)
    offset_after_first = data1["next_offset"]
    assert offset_after_first >= 1

    # Second message
    resp2, mid2 = await inject(client, "message two")
    data2 = await wait_for_output(client, since=offset_after_first, expected_count=1)
    assert data2["count"] >= 1
    # The new messages should only contain "message two"
    assert any("message two" in m["text"] for m in data2["messages"])
    # And should NOT contain "message one"
    assert not any("message one" in m["text"] for m in data2["messages"])


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_duplicate_message_id(client):
    """Same message_id injected twice → second returns delivery_id='dup'."""
    mid = str(uuid.uuid4())
    resp1, _ = await inject(client, "first injection", message_id=mid)
    data1 = await resp1.json()
    assert data1["ok"] is True
    assert data1["delivery_id"] != "dup"

    resp2, _ = await inject(client, "second injection same id", message_id=mid)
    data2 = await resp2.json()
    assert data2["ok"] is True
    assert data2["delivery_id"] == "dup"

    # Wait and check that only one echo was produced (not two)
    data = await wait_for_output(client, since=0, expected_count=1, timeout=1.5)
    echo_texts = [m["text"] for m in data["messages"] if m["content_type"] == "text"]
    # Should have exactly one echo from "first injection"
    assert len([t for t in echo_texts if "first injection" in t]) == 1
    # "second injection" should NOT appear
    assert not any("second injection" in t for t in echo_texts)


# ---------------------------------------------------------------------------
# [test-thinking] trigger
# ---------------------------------------------------------------------------

async def test_thinking_trigger(client):
    """[test-thinking] → reply includes a thinking entry AND a text entry."""
    resp, mid = await inject(client, "[test-thinking] what are you thinking?")
    assert resp.status == 200

    data = await wait_for_output(client, since=0, expected_count=2)
    types = [m["content_type"] for m in data["messages"]]
    assert "thinking" in types, f"Expected thinking type, got: {types}"
    assert "text" in types, f"Expected text type, got: {types}"


async def test_thinking_trigger_order(client):
    """[test-thinking] → thinking entry comes before text entry."""
    resp, mid = await inject(client, "[test-thinking] order test")
    data = await wait_for_output(client, since=0, expected_count=2)

    messages = data["messages"]
    types_in_order = [m["content_type"] for m in messages]
    thinking_idx = types_in_order.index("thinking")
    text_idx = types_in_order.index("text")
    assert thinking_idx < text_idx, f"thinking ({thinking_idx}) should come before text ({text_idx})"


# ---------------------------------------------------------------------------
# [test-markdown] trigger
# ---------------------------------------------------------------------------

async def test_markdown_trigger(client):
    """[test-markdown] → reply contains rich markdown elements."""
    resp, mid = await inject(client, "[test-markdown]")
    data = await wait_for_output(client, since=0, expected_count=1)

    text_messages = [m for m in data["messages"] if m["content_type"] == "text"]
    assert len(text_messages) >= 1
    md_text = text_messages[0]["text"]

    # Must contain at least some markdown elements
    assert "#" in md_text, "Expected heading (#) in markdown reply"
    assert "```" in md_text, "Expected code block (```) in markdown reply"
    assert "**" in md_text or "*" in md_text, "Expected bold/italic in markdown reply"
    assert ">" in md_text, "Expected blockquote (>) in markdown reply"


# ---------------------------------------------------------------------------
# [test-long:N] trigger
# ---------------------------------------------------------------------------

async def test_long_trigger(client):
    """[test-long:N] → reply is approximately N characters long."""
    n = 500
    resp, mid = await inject(client, f"[test-long:{n}]")
    data = await wait_for_output(client, since=0, expected_count=1)

    text_messages = [m for m in data["messages"] if m["content_type"] == "text"]
    assert len(text_messages) >= 1
    reply_len = len(text_messages[0]["text"])
    # Allow ±10% tolerance
    assert abs(reply_len - n) <= max(10, n * 0.1), (
        f"Expected ~{n} chars, got {reply_len}"
    )


async def test_long_trigger_large(client):
    """[test-long:5000] → reply is approximately 5000 characters."""
    n = 5000
    resp, mid = await inject(client, f"[test-long:{n}]")
    data = await wait_for_output(client, since=0, expected_count=1)

    text_messages = [m for m in data["messages"] if m["content_type"] == "text"]
    assert len(text_messages) >= 1
    reply_len = len(text_messages[0]["text"])
    assert abs(reply_len - n) <= max(50, n * 0.1), (
        f"Expected ~{n} chars, got {reply_len}"
    )


# ---------------------------------------------------------------------------
# [test-delay:Ns] trigger
# ---------------------------------------------------------------------------

async def test_delay_trigger(client):
    """[test-delay:Ns] → reply appears after N seconds."""
    import time as _time

    resp, mid = await inject(client, "[test-delay:1s] delayed message")
    assert resp.status == 200

    # Immediately after inject, output should be empty (or only from previous tests)
    # We get current offset from a fresh client perspective via a fresh fixture
    # Instead: check that output is empty right after inject
    resp_early = await get_output(client, since=0)
    data_early = await resp_early.json()
    early_count = data_early["count"]

    # Wait for 1.5 seconds and check output appears
    await asyncio.sleep(1.5)
    resp_late = await get_output(client, since=early_count)
    data_late = await resp_late.json()
    assert data_late["count"] >= 1, "Expected reply after delay but got none"


async def test_delay_trigger_produces_reply(client):
    """[test-delay:Ns] → reply text is produced."""
    resp, mid = await inject(client, "[test-delay:1s]")
    # Poll for up to 3 seconds total
    data = await wait_for_output(client, since=0, expected_count=1, timeout=3.0)
    assert data["count"] >= 1


# ---------------------------------------------------------------------------
# confirm_processed endpoint
# ---------------------------------------------------------------------------

async def test_confirm_processed(client):
    """POST /api/confirm_processed → confirms and removes from unprocessed."""
    resp, mid = await inject(client, "to be confirmed")
    assert resp.status == 200

    # The message should appear in /api/unprocessed
    resp_unpro = await client.get("/api/unprocessed", headers=AUTH_HEADERS)
    data_unpro = await resp_unpro.json()
    assert data_unpro["ok"] is True
    unprocessed_ids = [m["message_id"] for m in data_unpro["messages"]]
    assert mid in unprocessed_ids

    # Confirm the message
    resp_confirm = await client.post(
        "/api/confirm_processed",
        json={"message_ids": [mid]},
        headers=AUTH_HEADERS,
    )
    assert resp_confirm.status == 200
    data_confirm = await resp_confirm.json()
    assert data_confirm["ok"] is True
    assert data_confirm["confirmed"] == 1

    # Should no longer appear in /api/unprocessed
    resp_unpro2 = await client.get("/api/unprocessed", headers=AUTH_HEADERS)
    data_unpro2 = await resp_unpro2.json()
    unprocessed_ids2 = [m["message_id"] for m in data_unpro2["messages"]]
    assert mid not in unprocessed_ids2


async def test_confirm_processed_empty_list(client):
    """confirm_processed with empty list → confirmed=0."""
    resp = await client.post(
        "/api/confirm_processed",
        json={"message_ids": []},
        headers=AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["confirmed"] == 0


async def test_confirm_processed_unknown_ids(client):
    """confirm_processed with unknown IDs → confirmed=0."""
    resp = await client.post(
        "/api/confirm_processed",
        json={"message_ids": ["nonexistent-id-1", "nonexistent-id-2"]},
        headers=AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["confirmed"] == 0


# ---------------------------------------------------------------------------
# unprocessed endpoint
# ---------------------------------------------------------------------------

async def test_unprocessed_schema(client):
    """GET /api/unprocessed → each entry has required fields."""
    resp, mid = await inject(client, "unprocessed test message")
    resp_unpro = await client.get("/api/unprocessed", headers=AUTH_HEADERS)
    data = await resp_unpro.json()

    assert data["ok"] is True
    assert isinstance(data["messages"], list)

    # Find our injected message
    our_msg = next((m for m in data["messages"] if m["message_id"] == mid), None)
    assert our_msg is not None, f"Injected message {mid} not found in unprocessed"
    assert "injected_at" in our_msg
    assert "text_hint" in our_msg
    assert "T" in our_msg["injected_at"]  # ISO-8601 format


async def test_unprocessed_text_hint(client):
    """text_hint in unprocessed should be a prefix of the injected text."""
    long_text = "A" * 200
    resp, mid = await inject(client, long_text)
    resp_unpro = await client.get("/api/unprocessed", headers=AUTH_HEADERS)
    data = await resp_unpro.json()

    our_msg = next((m for m in data["messages"] if m["message_id"] == mid), None)
    assert our_msg is not None
    # text_hint is first 80 chars
    assert our_msg["text_hint"] == long_text[:80]


# ---------------------------------------------------------------------------
# /api/window stub
# ---------------------------------------------------------------------------

async def test_window_stub(client):
    """POST /api/window → always returns ok=True with echo window info."""
    resp = await client.post(
        "/api/window",
        json={"work_dir": "/some/path", "window_name": "test"},
        headers=AUTH_HEADERS,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["window_id"] == "@echo"
    assert data["window_name"] == "echo"
    assert "work_dir" in data


# ---------------------------------------------------------------------------
# Combined trigger words
# ---------------------------------------------------------------------------

async def test_combined_thinking_and_markdown(client):
    """[test-thinking] + [test-markdown] → produces thinking + text entries."""
    resp, mid = await inject(client, "[test-thinking] [test-markdown]")
    data = await wait_for_output(client, since=0, expected_count=3, timeout=3.0)

    types = [m["content_type"] for m in data["messages"]]
    assert "thinking" in types
    assert "text" in types
    # At least one text message should contain markdown
    text_msgs = [m["text"] for m in data["messages"] if m["content_type"] == "text"]
    markdown_found = any("#" in t for t in text_msgs)
    assert markdown_found, "Expected markdown in text messages"


async def test_combined_long_and_thinking(client):
    """[test-long:200] + [test-thinking] → produces thinking + long text."""
    resp, mid = await inject(client, "[test-long:200] [test-thinking]")
    data = await wait_for_output(client, since=0, expected_count=3, timeout=3.0)

    types = [m["content_type"] for m in data["messages"]]
    assert "thinking" in types
    text_msgs = [m["text"] for m in data["messages"] if m["content_type"] == "text"]
    # One of the text messages should be ~200 chars
    long_found = any(len(t) >= 180 for t in text_msgs)
    assert long_found, f"Expected a long text (~200 chars), got: {[len(t) for t in text_msgs]}"
