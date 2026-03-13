"""Tests for RecoveryManager."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from agentgate_gateway.recovery import RecoveryManager


def _make_manager(pending_in=None, pending_out=None, failed_out=None, unprocessed=None, failed_in=None):
    db = MagicMock()
    db.get_pending_inbound = AsyncMock(return_value=pending_in or [])
    db.get_pending_outbound = AsyncMock(return_value=pending_out or [])
    db.get_failed_outbound = AsyncMock(return_value=failed_out or [])
    db.get_unprocessed_for_backend = AsyncMock(return_value=unprocessed or [])
    db.get_failed_inbound_for_backend = AsyncMock(return_value=failed_in or [])
    db.update_inbound_process = AsyncMock()
    db.update_inbound_delivery = AsyncMock()

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
    msgs = [{"id": "f1", "content": "failed_reply"}]
    manager, db, inject_fn, repush_fn = _make_manager(failed_out=msgs)

    await manager.recover_on_startup()

    repush_fn.assert_called_once_with(msgs[0])


@pytest.mark.asyncio
async def test_backend_recovered_reinjects_unprocessed():
    """DB has unprocessed for backend → process_status updated to 'reinjected', inject_fn called."""
    msgs = [{"id": "u1", "content": "unprocessed_msg"}]
    manager, db, inject_fn, repush_fn = _make_manager(unprocessed=msgs)

    await manager.on_backend_recovered("backend_a")

    db.get_unprocessed_for_backend.assert_called_once_with("backend_a")
    db.update_inbound_process.assert_called_once_with("u1", "reinjected")
    inject_fn.assert_called_once_with(msgs[0])


@pytest.mark.asyncio
async def test_backend_recovered_retries_failed_messages():
    """Backend recovery retries delivery_status='failed' messages (Bug #7)."""
    failed_msgs = [
        {"id": "f1", "content": "failed_during_outage"},
        {"id": "f2", "content": "also_failed"},
    ]
    manager, db, inject_fn, repush_fn = _make_manager(failed_in=failed_msgs)

    await manager.on_backend_recovered("backend_a")

    db.get_failed_inbound_for_backend.assert_called_once_with("backend_a")
    # delivery_status reset to 'pending' before reinject
    assert db.update_inbound_delivery.call_count == 2
    db.update_inbound_delivery.assert_any_call("f1", "pending")
    db.update_inbound_delivery.assert_any_call("f2", "pending")
    # inject called for both
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
