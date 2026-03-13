"""Tests for SelfMonitor session recovery strategy.

Critical behavior under test (§4.5 of tech design):
  - Priority 1: `--resume {session_id}` when session_map.json has an entry
  - Priority 2: `--continue` fallback when no session_map entry exists

Also covers:
  - RestartBackoff: exponential backoff + circuit breaker
  - SelfMonitorConfig: loading from BackendConfig
  - check_all_windows: uses config.process_name, not hardcoded "claude"
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentgate_backend.config import BackendConfig
from agentgate_backend.self_monitor import (
    RestartBackoff,
    SelfMonitor,
    SelfMonitorConfig,
    _DEFAULT_CLAUDE_ERROR_PATTERNS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_instance_dir(tmp_path):
    """Provide a temporary instance directory with a BackendConfig pointing to it."""
    return tmp_path


@pytest.fixture
def session_map_file(tmp_instance_dir):
    """Return a session_map.json Path inside tmp_instance_dir."""
    return tmp_instance_dir / "session_map.json"


def _write_session_map(path: Path, tmux_session: str, window_id: str, session_id: str, window_name: str = ""):
    """Helper: write a minimal session_map.json."""
    key = f"{tmux_session}:{window_id}"
    data = {
        key: {
            "session_id": session_id,
            "cwd": "/tmp/test",
            "window_name": window_name,
        }
    }
    path.write_text(json.dumps(data))


@pytest.fixture
def mock_config(tmp_instance_dir, monkeypatch):
    """Patch _backend_config in self_monitor to use tmp paths."""
    cfg = BackendConfig(
        name="test",
        data_dir=tmp_instance_dir.parent,
        tmux_session_name="agentgate_test",
        claude_command="claude --dangerously-skip-permissions",
        process_name="claude",
        monitor_interval=30,
        restart_base_delay=5,
        restart_max_delay=300,
        restart_max_failures=3,
    )
    # Override the instance dir to point to tmp
    with patch("agentgate_backend.self_monitor._backend_config", cfg):
        # Make instance_dir and session_map_file point to tmp
        monkeypatch.setattr(cfg, "instance_dir", tmp_instance_dir, raising=False)
        monkeypatch.setattr(
            cfg, "session_map_file",
            property(lambda self: tmp_instance_dir / "session_map.json"),
            raising=False,
        )
        # Direct attribute set since property can't be monkeypatched on instance
        # Use __dict__ to bypass pydantic
        object.__setattr__(cfg, "_session_map_file_override", tmp_instance_dir / "session_map.json")
        yield cfg


# ---------------------------------------------------------------------------
# RestartBackoff tests
# ---------------------------------------------------------------------------


class TestRestartBackoff:
    def test_initial_state_allows_restart(self):
        b = RestartBackoff(base_delay=1, max_delay=10, max_failures=3)
        assert b.may_restart("key1") is True
        assert b.is_circuit_open("key1") is False
        assert b.failure_count("key1") == 0

    def test_first_failure_increases_delay(self):
        b = RestartBackoff(base_delay=60, max_delay=3600, max_failures=5)
        b.record_failure("key1")
        assert b.failure_count("key1") == 1
        # After first failure, must wait base_delay before next attempt
        assert b.may_restart("key1") is False

    def test_circuit_opens_after_max_failures(self):
        b = RestartBackoff(base_delay=1, max_delay=10, max_failures=3)
        for _ in range(3):
            b.record_failure("key1")
        assert b.is_circuit_open("key1") is True
        assert b.may_restart("key1") is False

    def test_circuit_does_not_open_before_max_failures(self):
        b = RestartBackoff(base_delay=60, max_delay=3600, max_failures=3)
        for _ in range(2):
            b.record_failure("key1")
        assert b.is_circuit_open("key1") is False

    def test_success_resets_state(self):
        b = RestartBackoff(base_delay=1, max_delay=10, max_failures=3)
        for _ in range(2):
            b.record_failure("key1")
        b.record_success("key1")
        assert b.failure_count("key1") == 0
        assert b.may_restart("key1") is True
        assert b.is_circuit_open("key1") is False

    def test_independent_keys(self):
        b = RestartBackoff(base_delay=1, max_delay=10, max_failures=3)
        b.record_failure("key1")
        b.record_failure("key1")
        b.record_failure("key1")
        assert b.is_circuit_open("key1") is True
        # key2 is unaffected
        assert b.may_restart("key2") is True
        assert b.is_circuit_open("key2") is False


# ---------------------------------------------------------------------------
# SelfMonitorConfig tests
# ---------------------------------------------------------------------------


class TestSelfMonitorConfig:
    def test_from_backend_config_uses_monitor_interval(self):
        cfg = BackendConfig(monitor_interval=45)
        with patch("agentgate_backend.self_monitor._backend_config", cfg):
            smc = SelfMonitorConfig.from_backend_config()
        assert smc.interval == 45
        assert smc.enabled is True
        assert smc.on_claude_dead == "restart"
        assert smc.on_claude_degraded == "kill_and_restart"

    def test_from_backend_config_defaults(self):
        cfg = BackendConfig()
        with patch("agentgate_backend.self_monitor._backend_config", cfg):
            smc = SelfMonitorConfig.from_backend_config()
        # Default monitor_interval is 30
        assert smc.interval == 30
        assert smc.degraded_threshold == 3


# ---------------------------------------------------------------------------
# SelfMonitor._get_session_id_by_window_id tests (session recovery strategy)
# ---------------------------------------------------------------------------


class TestSessionIdLookup:
    """Test the session_id lookup methods that drive recovery command selection."""

    def _make_monitor(self, tmp_path, tmux_session="agentgate_test"):
        cfg = BackendConfig(
            name="test",
            tmux_session_name=tmux_session,
            claude_command="claude",
            process_name="claude",
        )
        # Override session_map_file to point to tmp
        session_map_path = tmp_path / "session_map.json"
        object.__setattr__(cfg, "_test_session_map", session_map_path)

        monitor_config = SelfMonitorConfig()
        tmux_mock = MagicMock()

        with patch("agentgate_backend.self_monitor._backend_config", cfg):
            # Monkeypatch the config's session_map_file property
            with patch.object(
                type(cfg), "session_map_file",
                new_callable=lambda: property(lambda self: session_map_path),
            ):
                monitor = SelfMonitor(
                    tmux_manager=tmux_mock,
                    config=monitor_config,
                )
                monitor._cfg_ref = cfg
                monitor._session_map_path = session_map_path
        return monitor, cfg, session_map_path

    def test_returns_none_when_no_session_map_file(self, tmp_path):
        monitor, cfg, session_map_path = self._make_monitor(tmp_path)
        # File does not exist
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            result = monitor._get_session_id_by_window_id("@5")
        assert result is None

    def test_returns_session_id_when_entry_exists(self, tmp_path):
        monitor, cfg, session_map_path = self._make_monitor(tmp_path)
        _write_session_map(
            session_map_path,
            tmux_session="agentgate_test",
            window_id="@5",
            session_id="abc-123",
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            result = monitor._get_session_id_by_window_id("@5")
        assert result == "abc-123"

    def test_returns_none_for_different_window_id(self, tmp_path):
        monitor, cfg, session_map_path = self._make_monitor(tmp_path)
        _write_session_map(
            session_map_path,
            tmux_session="agentgate_test",
            window_id="@5",
            session_id="abc-123",
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            result = monitor._get_session_id_by_window_id("@9")
        assert result is None

    def test_returns_none_for_different_tmux_session(self, tmp_path):
        """Entries from a different tmux session should not match."""
        monitor, cfg, session_map_path = self._make_monitor(tmp_path)
        _write_session_map(
            session_map_path,
            tmux_session="other_session",
            window_id="@5",
            session_id="abc-123",
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            result = monitor._get_session_id_by_window_id("@5")
        assert result is None

    def test_handles_malformed_session_map_json(self, tmp_path):
        monitor, cfg, session_map_path = self._make_monitor(tmp_path)
        session_map_path.write_text("not valid json {{{")
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            result = monitor._get_session_id_by_window_id("@5")
        assert result is None


# ---------------------------------------------------------------------------
# SelfMonitor._build_restart_command tests (THE CRITICAL BEHAVIOR)
# ---------------------------------------------------------------------------


class TestBuildRestartCommand:
    """Test that _build_restart_command produces the correct recovery command."""

    def _make_monitor_with_map(self, tmp_path, session_id=None, window_id="@3", window_name="dev"):
        cfg = BackendConfig(
            name="test",
            tmux_session_name="agentgate_test",
            claude_command="claude --dangerously-skip-permissions",
            process_name="claude",
        )
        session_map_path = tmp_path / "session_map.json"

        if session_id:
            _write_session_map(
                session_map_path,
                tmux_session="agentgate_test",
                window_id=window_id,
                session_id=session_id,
                window_name=window_name,
            )

        monitor_config = SelfMonitorConfig()
        tmux_mock = MagicMock()
        monitor = SelfMonitor.__new__(SelfMonitor)
        monitor._tmux = tmux_mock
        monitor._config = monitor_config
        monitor._alert_fn = None
        monitor._claude_command = cfg.claude_command
        monitor._backoff = RestartBackoff()
        monitor._task = None
        monitor._consecutive_degraded = {}
        monitor._prev_alive = {}
        monitor._window_statuses = {}

        return monitor, cfg, session_map_path

    def test_uses_resume_when_session_id_in_map(self, tmp_path):
        """Priority 1: uses --resume {session_id} when session_map has entry."""
        monitor, cfg, session_map_path = self._make_monitor_with_map(
            tmp_path, session_id="sid-xyz-789", window_id="@3"
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd = monitor._build_restart_command("@3", "dev")
        assert "--resume sid-xyz-789" in cmd
        assert "--continue" not in cmd

    def test_falls_back_to_continue_when_no_session_map(self, tmp_path):
        """Priority 2: falls back to --continue when no session_map entry exists."""
        monitor, cfg, session_map_path = self._make_monitor_with_map(
            tmp_path, session_id=None, window_id="@3"
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd = monitor._build_restart_command("@3", "dev")
        assert "--continue" in cmd
        assert "--resume" not in cmd

    def test_falls_back_to_continue_when_file_missing(self, tmp_path):
        """Priority 2: falls back when session_map.json doesn't exist."""
        monitor, cfg, session_map_path = self._make_monitor_with_map(
            tmp_path, session_id=None
        )
        # Ensure file doesn't exist
        if session_map_path.exists():
            session_map_path.unlink()
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd = monitor._build_restart_command("@3", "dev")
        assert "--continue" in cmd
        assert "--resume" not in cmd

    def test_resume_command_includes_base_command(self, tmp_path):
        """The base claude_command prefix is preserved in the restart command."""
        monitor, cfg, session_map_path = self._make_monitor_with_map(
            tmp_path, session_id="sid-abc", window_id="@7"
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd = monitor._build_restart_command("@7", "test-window")
        # Base command preserved
        assert cmd.startswith("claude --dangerously-skip-permissions")
        assert "--resume sid-abc" in cmd

    def test_continue_command_includes_base_command(self, tmp_path):
        """The base claude_command prefix is preserved in the fallback command."""
        monitor, cfg, session_map_path = self._make_monitor_with_map(
            tmp_path, session_id=None
        )
        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd = monitor._build_restart_command("@7", "test-window")
        assert cmd.startswith("claude --dangerously-skip-permissions")
        assert "--continue" in cmd

    def test_different_windows_get_different_session_ids(self, tmp_path):
        """Each window should get its own session_id from session_map."""
        cfg = BackendConfig(
            name="test",
            tmux_session_name="agentgate_test",
            claude_command="claude",
            process_name="claude",
        )
        session_map_path = tmp_path / "session_map.json"
        # Two windows with different session IDs
        data = {
            "agentgate_test:@1": {"session_id": "sid-window-1", "cwd": "/tmp/a"},
            "agentgate_test:@2": {"session_id": "sid-window-2", "cwd": "/tmp/b"},
        }
        session_map_path.write_text(json.dumps(data))

        monitor = SelfMonitor.__new__(SelfMonitor)
        monitor._tmux = MagicMock()
        monitor._config = SelfMonitorConfig()
        monitor._alert_fn = None
        monitor._claude_command = "claude"
        monitor._backoff = RestartBackoff()
        monitor._task = None
        monitor._consecutive_degraded = {}
        monitor._prev_alive = {}
        monitor._window_statuses = {}

        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            cmd1 = monitor._build_restart_command("@1", "win1")
            cmd2 = monitor._build_restart_command("@2", "win2")

        assert "sid-window-1" in cmd1
        assert "sid-window-2" in cmd2
        assert "sid-window-1" not in cmd2
        assert "sid-window-2" not in cmd1


# ---------------------------------------------------------------------------
# SelfMonitor.check_all_windows: uses config.process_name not hardcoded "claude"
# ---------------------------------------------------------------------------


class TestCheckAllWindowsProcessName:
    """Verify check_all_windows uses config.process_name, not hardcoded 'claude'."""

    @pytest.mark.asyncio
    async def test_uses_process_name_from_config(self, tmp_path):
        cfg = BackendConfig(
            name="test",
            tmux_session_name="agentgate_test",
            claude_command="opencode",
            process_name="opencode",  # Different from "claude"
        )
        session_map_path = tmp_path / "session_map.json"

        monitor_config = SelfMonitorConfig()
        tmux_mock = AsyncMock()

        # Simulate a window running "opencode" (our process_name)
        mock_window = MagicMock()
        mock_window.window_id = "@1"
        mock_window.window_name = "dev"
        mock_window.pane_current_command = "opencode"
        tmux_mock.list_windows = AsyncMock(return_value=[mock_window])
        tmux_mock.capture_pane = AsyncMock(return_value="")

        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            monitor = SelfMonitor(
                tmux_manager=tmux_mock,
                config=monitor_config,
            )
            result = await monitor.check_all_windows()

        # Window running "opencode" (= process_name) should be "ok", not "dead"
        assert "@1" in result
        assert result["@1"]["status"] == "ok", (
            f"Expected 'ok' but got '{result['@1']['status']}' — "
            f"check_all_windows must use config.process_name not hardcoded 'claude'"
        )

    @pytest.mark.asyncio
    async def test_dead_when_process_name_mismatch(self, tmp_path):
        cfg = BackendConfig(
            name="test",
            tmux_session_name="agentgate_test",
            claude_command="claude",
            process_name="claude",
        )
        session_map_path = tmp_path / "session_map.json"

        monitor_config = SelfMonitorConfig()
        tmux_mock = AsyncMock()

        # Window running "bash" (not process_name)
        mock_window = MagicMock()
        mock_window.window_id = "@2"
        mock_window.window_name = "dev"
        mock_window.pane_current_command = "bash"
        tmux_mock.list_windows = AsyncMock(return_value=[mock_window])

        with patch("agentgate_backend.self_monitor._backend_config", cfg), \
             patch.object(type(cfg), "session_map_file",
                          new_callable=lambda: property(lambda self: session_map_path)):
            monitor = SelfMonitor(
                tmux_manager=tmux_mock,
                config=monitor_config,
            )
            result = await monitor.check_all_windows()

        assert "@2" in result
        assert result["@2"]["status"] == "dead"


# ---------------------------------------------------------------------------
# Error pattern tests
# ---------------------------------------------------------------------------


class TestErrorPatterns:
    def test_default_patterns_are_defined(self):
        assert len(_DEFAULT_CLAUDE_ERROR_PATTERNS) > 0
        for pattern, label in _DEFAULT_CLAUDE_ERROR_PATTERNS:
            assert isinstance(pattern, str) and pattern
            assert isinstance(label, str) and label

    def test_known_patterns_present(self):
        patterns_only = [p for p, _ in _DEFAULT_CLAUDE_ERROR_PATTERNS]
        assert "rate limit" in patterns_only
        assert "overloaded" in patterns_only
        assert "not logged in" in patterns_only
