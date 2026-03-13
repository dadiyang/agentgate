"""Tests for HealthProber."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentgate_gateway.health_prober import BackendState, HealthProber


def _make_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


def _make_prober(state: BackendState, on_recovered=None, on_unhealthy=None):
    on_recovered = on_recovered or AsyncMock()
    on_unhealthy = on_unhealthy or AsyncMock()
    backends = {"b1": state}
    prober = HealthProber(
        backends=backends,
        on_recovered=on_recovered,
        on_unhealthy=on_unhealthy,
    )
    return prober, on_recovered, on_unhealthy


@pytest.mark.asyncio
async def test_healthy_stays_healthy():
    """Probe returns 200 → status remains healthy, fail_count stays 0."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "healthy"
    state.fail_count = 0

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(200))

    await prober._probe("b1", state)

    assert state.status == "healthy"
    assert state.fail_count == 0
    on_recovered.assert_not_called()
    on_unhealthy.assert_not_called()


@pytest.mark.asyncio
async def test_unhealthy_after_3_failures():
    """3 consecutive failures → status becomes unhealthy, on_unhealthy called once."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unknown"

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(500))

    for _ in range(3):
        await prober._probe("b1", state)

    assert state.status == "unhealthy"
    assert state.fail_count == 3
    on_unhealthy.assert_called_once_with("b1")
    on_recovered.assert_not_called()


@pytest.mark.asyncio
async def test_single_failure_not_unhealthy():
    """1 failure → still not unhealthy, fail_count=1."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unknown"

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(503))

    await prober._probe("b1", state)

    assert state.status == "unknown"
    assert state.fail_count == 1
    on_unhealthy.assert_not_called()
    on_recovered.assert_not_called()


@pytest.mark.asyncio
async def test_unhealthy_to_healthy_triggers_recovery():
    """Was unhealthy, probe succeeds → on_recovered called."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unhealthy"
    state.fail_count = 5

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(200))

    await prober._probe("b1", state)

    assert state.status == "healthy"
    assert state.fail_count == 0
    assert state.last_error is None
    on_recovered.assert_called_once_with("b1")
    on_unhealthy.assert_not_called()


@pytest.mark.asyncio
async def test_healthy_probe_resets_fail_count():
    """fail_count was 2, probe succeeds → fail_count=0."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unknown"
    state.fail_count = 2

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(200))

    await prober._probe("b1", state)

    assert state.fail_count == 0
    assert state.status == "healthy"
    on_recovered.assert_not_called()  # was not unhealthy
    on_unhealthy.assert_not_called()


@pytest.mark.asyncio
async def test_already_unhealthy_no_duplicate_callback():
    """Already unhealthy, more failures → on_unhealthy NOT called again."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unhealthy"
    state.fail_count = 3

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(return_value=_make_response(500))

    # Three more probes — all fail
    for _ in range(3):
        await prober._probe("b1", state)

    assert state.status == "unhealthy"
    on_unhealthy.assert_not_called()


@pytest.mark.asyncio
async def test_probe_timeout_counts_as_failure():
    """httpx.get raises exception → fail_count increments."""
    state = BackendState(url="http://backend", api_token="tok")
    state.status = "unknown"

    prober, on_recovered, on_unhealthy = _make_prober(state)
    prober._http.get = AsyncMock(
        side_effect=httpx.TimeoutException("connect timeout")
    )

    await prober._probe("b1", state)

    assert state.fail_count == 1
    assert state.last_error == "connect timeout"
    on_unhealthy.assert_not_called()  # only 1 failure, threshold is 3
