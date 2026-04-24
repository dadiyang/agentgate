"""Tests for agentgate_gateway.router"""

import pytest

from agentgate_gateway.config import RouteConfig
from agentgate_gateway.router import Router


def make_route(channel: str, bot_id: str, chat_id: str, backend: str) -> RouteConfig:
    return RouteConfig(channel=channel, bot_id=bot_id, chat_id=chat_id, backend=backend)


class TestMatch:
    def test_exact_match_returns_backend_id(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        result = router.match("feishu", "bot1", "grp1")
        assert result == "fish-dev"

    def test_no_match_returns_none(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        result = router.match("feishu", "bot1", "grp_unknown")
        assert result is None

    def test_wrong_channel_returns_none(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        assert router.match("telegram", "bot1", "grp1") is None

    def test_wrong_bot_id_returns_none(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        assert router.match("feishu", "bot2", "grp1") is None

    def test_empty_routes_always_returns_none(self):
        router = Router([])
        assert router.match("feishu", "bot1", "grp1") is None

    def test_multiple_routes_correct_dispatch(self):
        routes = [
            make_route("feishu", "bot1", "grp-fish", "fish-dev"),
            make_route("feishu", "bot1", "grp-trade", "trade-dev"),
            make_route("telegram", "tgbot", "chat123", "fish-tg"),
        ]
        router = Router(routes)
        assert router.match("feishu", "bot1", "grp-fish") == "fish-dev"
        assert router.match("feishu", "bot1", "grp-trade") == "trade-dev"
        assert router.match("telegram", "tgbot", "chat123") == "fish-tg"

    def test_partial_match_not_enough(self):
        """All three fields must match; two out of three is not a match."""
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        assert router.match("feishu", "bot1", "") is None
        assert router.match("feishu", "", "grp1") is None
        assert router.match("", "bot1", "grp1") is None


class TestReverseLookup:
    def test_reverse_lookup_single_binding(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        bindings = router.reverse_lookup("fish-dev")
        assert len(bindings) == 1
        assert bindings[0] == ("feishu", "bot1", "grp1")

    def test_reverse_lookup_multiple_bindings(self):
        routes = [
            make_route("feishu", "bot1", "grp1", "fish-dev"),
            make_route("telegram", "tgbot", "chat1", "fish-dev"),
        ]
        router = Router(routes)
        bindings = router.reverse_lookup("fish-dev")
        assert len(bindings) == 2
        assert ("feishu", "bot1", "grp1") in bindings
        assert ("telegram", "tgbot", "chat1") in bindings

    def test_reverse_lookup_unknown_backend_returns_empty(self):
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        assert router.reverse_lookup("nonexistent") == []

    def test_reverse_lookup_empty_routes(self):
        router = Router([])
        assert router.reverse_lookup("any-backend") == []

    def test_reverse_lookup_does_not_mix_backends(self):
        routes = [
            make_route("feishu", "bot1", "grp1", "fish-dev"),
            make_route("feishu", "bot2", "grp2", "trade-dev"),
        ]
        router = Router(routes)
        fish_bindings = router.reverse_lookup("fish-dev")
        trade_bindings = router.reverse_lookup("trade-dev")
        assert len(fish_bindings) == 1
        assert fish_bindings[0] == ("feishu", "bot1", "grp1")
        assert len(trade_bindings) == 1
        assert trade_bindings[0] == ("feishu", "bot2", "grp2")

    def test_reverse_lookup_returns_new_list_not_internal_ref(self):
        """Mutating the returned list must not affect internal state."""
        routes = [make_route("feishu", "bot1", "grp1", "fish-dev")]
        router = Router(routes)
        bindings = router.reverse_lookup("fish-dev")
        bindings.clear()
        assert len(router.reverse_lookup("fish-dev")) == 1
