"""Tests for RecoveryManager."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from agentgate_gateway.recovery import RecoveryManager


def _make_manager(pending_in=None, pending_out=None, failed_out=None, failed_in=None):
    db = MagicMock()

    async def _get_pending(direction):
        return {"inbound": pending_in or [], "outbound": pending_out or []}.get(direction, [])

    async def _get_failed(direction, backend_id=None):
        if direction == "outbound":
            return failed_out or []
        if direction == "inbound":
            return failed_in or []
        return []

    db.get_pending = _get_pending
    db.get_failed = _get_failed
    db.update_status = AsyncMock()

    inject_fn = AsyncMock()
    repush_fn = AsyncMock()

    manager = RecoveryManager(db=db, inject_fn=inject_fn, repush_fn=repush_fn)
    return manager, db, inject_fn, repush_fn


@pytest.mark.asyncio
async def test_startup_recovery_pending_inbound():
    """DB has pending inbound → inject_fn called for each."""
    msgs = [{"id": "m1", "content": "hello"}, {"id": "m2", "content": "world"}]
    manager, db, inject_fn, repush_fn = _make_manager(pending_in=msgs)

    await manager.recover_on_startup()

    assert inject_fn.call_count == 2
    inject_fn.assert_any_call(msgs[0])
    inject_fn.assert_any_call(msgs[1])
    repush_fn.assert_not_called()


@pytest.mark.asyncio
async def test_startup_recovery_pending_outbound():
    """DB has pending outbound → repush_fn called for each."""
    msgs = [{"id": "o1", "content": "reply1"}, {"id": "o2", "content": "reply2"}]
    manager, db, inject_fn, repush_fn = _make_manager(pending_out=msgs)

    await manager.recover_on_startup()

    assert repush_fn.call_count == 2
    repush_fn.assert_any_call(msgs[0])
    repush_fn.assert_any_call(msgs[1])


@pytest.mark.asyncio
async def test_startup_recovery_failed_outbound():
    """DB has failed outbound → repush_fn called."""
    msgs = [{"id": "f1", "content": "failed_reply", "retry_count": 0}]
    manager, db, inject_fn, repush_fn = _make_manager(failed_out=msgs)

    await manager.recover_on_startup()

    repush_fn.assert_called_once_with(msgs[0])


@pytest.mark.asyncio
async def test_backend_recovered_reinjects_failed():
    """Backend recovered: failed inbound status reset to 'pending', inject_fn called."""
    msgs = [{"id": "u1", "content": "unprocessed_msg"}]
    manager, db, inject_fn, repush_fn = _make_manager(failed_in=msgs)

    await manager.on_backend_recovered("backend_a")

    db.update_status.assert_called_once_with("u1", "pending")
    inject_fn.assert_called_once_with(msgs[0])


@pytest.mark.asyncio
async def test_backend_recovered_retries_multiple_failed_messages():
    """Backend recovery retries all failed inbound messages for that backend."""
    failed_msgs = [
        {"id": "f1", "content": "failed_during_outage"},
        {"id": "f2", "content": "also_failed"},
    ]
    manager, db, inject_fn, repush_fn = _make_manager(failed_in=failed_msgs)

    await manager.on_backend_recovered("backend_a")

    assert db.update_status.call_count == 2
    db.update_status.assert_any_call("f1", "pending")
    db.update_status.assert_any_call("f2", "pending")
    assert inject_fn.call_count == 2
    inject_fn.assert_any_call(failed_msgs[0])
    inject_fn.assert_any_call(failed_msgs[1])


@pytest.mark.asyncio
async def test_recovery_error_doesnt_stop_others():
    """One message fails to reinject → continues with remaining messages."""
    msgs = [
        {"id": "e1", "content": "will_fail"},
        {"id": "e2", "content": "will_succeed"},
        {"id": "e3", "content": "will_also_succeed"},
    ]
    manager, db, inject_fn, repush_fn = _make_manager(pending_in=msgs)

    # First message raises, others succeed
    inject_fn.side_effect = [Exception("network error"), None, None]

    await manager.recover_on_startup()

    # All three were attempted
    assert inject_fn.call_count == 3
