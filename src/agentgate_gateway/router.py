"""Exact triple routing: (channel, bot_id, chat_id) → backend_id."""

import logging

from agentgate_gateway.config import RouteConfig

logger = logging.getLogger(__name__)


class Router:
    def __init__(self, routes: list[RouteConfig]) -> None:
        # Forward: (channel, bot_id, chat_id) → backend_id
        self._forward: dict[tuple[str, str, str], str] = {}
        # Reverse: backend_id → list of (channel, bot_id, chat_id)
        self._reverse: dict[str, list[tuple[str, str, str]]] = {}

        for route in routes:
            key = (route.channel, route.bot_id, route.chat_id)
            self._forward[key] = route.backend
            self._reverse.setdefault(route.backend, []).append(key)

        # Warn on N:1 routes (multiple channels → same backend).
        # Output is broadcast to ALL bound channels, which is usually
        # not desired (PRD F04 assumes 1:1).
        for bid, bindings in self._reverse.items():
            if len(bindings) > 1:
                channels = ", ".join(
                    f"({ch}/{gid})" for ch, _, gid in bindings
                )
                logger.warning(
                    "Backend '%s' has %d routes: %s — output will be "
                    "broadcast to ALL bound channels. Use separate backends "
                    "for 1:1 isolation.",
                    bid, len(bindings), channels,
                )

    def match(self, channel: str, bot_id: str, chat_id: str) -> str | None:
        """Exact match only. Returns None if no route found (silent ignore per AC-10)."""
        return self._forward.get((channel, bot_id, chat_id))

    def reverse_lookup(self, backend_id: str) -> list[tuple[str, str, str]]:
        """For outbound: find all channel bindings for a backend."""
        return list(self._reverse.get(backend_id, []))

    def reload(self, routes: list[RouteConfig]) -> int:
        """Replace routing table in-place. Returns number of routes loaded."""
        forward: dict[tuple[str, str, str], str] = {}
        reverse: dict[str, list[tuple[str, str, str]]] = {}
        for route in routes:
            key = (route.channel, route.bot_id, route.chat_id)
            forward[key] = route.backend
            reverse.setdefault(route.backend, []).append(key)
        self._forward = forward
        self._reverse = reverse
        logger.info("Router reloaded: %d routes", len(forward))
        return len(forward)
