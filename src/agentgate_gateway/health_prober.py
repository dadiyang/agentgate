"""L2 backend health probing for AgentGate Gateway."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class BackendState:
    """Runtime state for a backend instance."""

    def __init__(self, url: str, api_token: str, default_window: str = "main"):
        self.url = url
        self.api_token = api_token
        self.default_window = default_window
        self.status: str = "unknown"  # healthy / unhealthy / unknown
        self.fail_count: int = 0
        self.last_check: str | None = None
        self.last_error: str | None = None


class HealthProber:
    CONSECUTIVE_FAIL = 3  # Mark unhealthy after 3 consecutive failures
    TIMEOUT = 10  # Probe timeout in seconds

    def __init__(
        self,
        backends: dict[str, BackendState],
        on_recovered: Callable[[str], Awaitable[None]],
        on_unhealthy: Callable[[str], Awaitable[None]],
        probe_interval: float = 30.0,
        probe_interval_low: float = 60.0,
    ):
        self._backends = backends
        self._on_recovered = on_recovered
        self._on_unhealthy = on_unhealthy
        self._probe_interval = probe_interval
        self._probe_interval_low = probe_interval_low
        self._http = httpx.AsyncClient(timeout=self.TIMEOUT)
        self._running = True

    async def close(self):
        await self._http.aclose()

    async def run(self):
        while self._running:
            for backend_id, state in self._backends.items():
                await self._probe(backend_id, state)
            # Use lower interval if any backend is unhealthy
            has_unhealthy = any(s.status == "unhealthy" for s in self._backends.values())
            interval = self._probe_interval_low if has_unhealthy else self._probe_interval
            await asyncio.sleep(interval)

    async def _probe(self, backend_id: str, state: BackendState):
        state.last_check = datetime.now(timezone.utc).isoformat()
        try:
            resp = await self._http.get(
                f"{state.url}/api/health",
                headers={"Authorization": f"Bearer {state.api_token}"},
            )
            if resp.status_code == 200:
                was_unhealthy = state.status == "unhealthy"
                state.status = "healthy"
                state.fail_count = 0
                state.last_error = None
                if was_unhealthy:
                    await self._on_recovered(backend_id)
                return
            state.fail_count += 1
            state.last_error = f"HTTP {resp.status_code}"
        except Exception as e:
            state.fail_count += 1
            state.last_error = str(e)

        if state.fail_count >= self.CONSECUTIVE_FAIL and state.status != "unhealthy":
            state.status = "unhealthy"
            await self._on_unhealthy(backend_id)

    def stop(self):
        self._running = False
