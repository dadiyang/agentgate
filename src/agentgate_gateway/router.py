"""Exact triple routing: (channel, bot_id, group_id) → backend_id."""

from agentgate_gateway.config import RouteConfig


class Router:
    def __init__(self, routes: list[RouteConfig]) -> None:
        # Forward: (channel, bot_id, group_id) → backend_id
        self._forward: dict[tuple[str, str, str], str] = {}
        # Reverse: backend_id → list of (channel, bot_id, group_id)
        self._reverse: dict[str, list[tuple[str, str, str]]] = {}

        for route in routes:
            key = (route.channel, route.bot_id, route.group_id)
            self._forward[key] = route.backend
            self._reverse.setdefault(route.backend, []).append(key)

    def match(self, channel: str, bot_id: str, group_id: str) -> str | None:
        """Exact match only. Returns None if no route found (silent ignore per AC-10)."""
        return self._forward.get((channel, bot_id, group_id))

    def reverse_lookup(self, backend_id: str) -> list[tuple[str, str, str]]:
        """For outbound: find all channel bindings for a backend."""
        return list(self._reverse.get(backend_id, []))
