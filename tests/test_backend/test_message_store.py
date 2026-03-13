"""Tests for MessageStore — idempotency store for inject message IDs."""

import time
from unittest.mock import patch

import pytest

from agentgate_backend.message_store import MessageStore


class TestMessageStore:
    def test_add_and_has(self):
        """add() followed by has() returns True."""
        store = MessageStore()
        store.add("msg-001")
        assert store.has("msg-001") is True

    def test_not_found(self):
        """has() returns False for an unknown message_id."""
        store = MessageStore()
        assert store.has("unknown-id") is False

    def test_duplicate_detection(self):
        """Adding the same message_id twice; has() returns True both times."""
        store = MessageStore()
        store.add("dup-id")
        store.add("dup-id")  # second add — should not error
        assert store.has("dup-id") is True

    def test_ttl_cleanup(self):
        """Expired entries are cleaned up; has() returns False after TTL passes."""
        store = MessageStore(ttl=1)
        store.add("expire-me")
        assert store.has("expire-me") is True

        # Advance time past the TTL by patching time.time
        future = time.time() + 2
        with patch("agentgate_backend.message_store.time") as mock_time:
            mock_time.time.return_value = future
            result = store.has("expire-me")

        assert result is False

    def test_remove(self):
        """add() then remove() — has() returns False."""
        store = MessageStore()
        store.add("removable")
        assert store.has("removable") is True
        store.remove("removable")
        assert store.has("removable") is False

    def test_remove_nonexistent_is_idempotent(self):
        """remove() on unknown message_id does not raise."""
        store = MessageStore()
        store.remove("ghost-id")  # must not raise

    def test_multiple_independent_ids(self):
        """Different message_ids are tracked independently."""
        store = MessageStore()
        store.add("id-a")
        store.add("id-b")
        assert store.has("id-a") is True
        assert store.has("id-b") is True
        assert store.has("id-c") is False

        store.remove("id-a")
        assert store.has("id-a") is False
        assert store.has("id-b") is True
