"""Built-in SelfMonitor — watches claude process health and auto-recovers.

Replaces the external watchdog with an in-process monitor that can react faster
and without cross-process tmux operations.

Key classes:
  - RestartBackoff: Exponential backoff + circuit breaker for restarts.
  - SelfMonitorConfig: Configuration dataclass (from BackendConfig).
  - SelfMonitor: Main monitoring loop (asyncio.Task).

Session recovery strategy (§4.5 of tech design):
  - Priority 1: `--resume {session_id}` from session_map.json
  - Priority 2: `--continue` (fallback for first launch / no session_map entry)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from .config import config as _backend_config

if TYPE_CHECKING:
    from .tmux_manager import TmuxManager

logger = logging.getLogger(__name__)

# Claude Code error patterns in pane content (case-insensitive).
# Each: (pattern, label). Checked against last ~10 lines of pane output.
# Configurable via BackendConfig.claude_error_patterns (if extended later);
# defaults here cover the standard set verified in ccbot production.
_DEFAULT_CLAUDE_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("rate limit", "API rate limited"),
    ("overloaded", "API overloaded"),
    ("run /login", "Auth expired"),
    ("not logged in", "Auth expired"),
    ("econnrefused", "Connection refused"),
    ("etimedout", "Connection timed out"),
    ("api error", "API error"),
    ("account does not have", "Account issue"),
]


def _get_error_patterns() -> list[tuple[str, str]]:
    """Return error patterns, from config if available, else defaults."""
    patterns = getattr(_backend_config, "claude_error_patterns", None)
    if patterns:
        return patterns
    return _DEFAULT_CLAUDE_ERROR_PATTERNS


class RestartBackoff:
    """Exponential backoff + circuit breaker for restart operations.

    Schedule: 0 -> base -> base*2 -> base*4 -> ... -> max_delay (cap).
    Circuit opens after max_failures consecutive failures.
    """

    def __init__(
        self,
        base_delay: float = 60,
        max_delay: float = 3600,
        max_failures: int = 5,
    ) -> None:
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_failures = max_failures
        self._state: dict[str, dict[str, Any]] = {}

    def may_restart(self, key: str) -> bool:
        """Return True if a restart attempt is allowed right now."""
        s = self._state.get(key)
        if not s:
            return True
        if s["count"] >= self.max_failures:
            return False
        return time.monotonic() >= s["next_allowed"]

    def is_circuit_open(self, key: str) -> bool:
        """Return True if too many failures — no more auto-restarts."""
        s = self._state.get(key)
        return s is not None and s["count"] >= self.max_failures

    def record_failure(self, key: str) -> None:
        """Record a restart failure, increasing backoff delay."""
        s = self._state.get(key, {"count": 0, "next_allowed": 0.0})
        s["count"] += 1
        delay = min(self.base_delay * (2 ** (s["count"] - 1)), self.max_delay)
        s["next_allowed"] = time.monotonic() + delay
        self._state[key] = s

    def record_success(self, key: str) -> None:
        """Clear backoff state after successful recovery."""
        self._state.pop(key, None)

    def failure_count(self, key: str) -> int:
        """Return current consecutive failure count for a key."""
        s = self._state.get(key)
        return s["count"] if s else 0


@dataclass
class SelfMonitorConfig:
    """Configuration for the self-monitor, loaded from BackendConfig."""

    enabled: bool = True
    interval: int = 60
    on_claude_dead: str = "restart"  # restart / kill_and_restart / notify / ignore
    on_claude_degraded: str = "kill_and_restart"
    degraded_threshold: int = 3

    @classmethod
    def from_backend_config(cls) -> "SelfMonitorConfig":
        """Load config from the BackendConfig singleton."""
        cfg = _backend_config
        return cls(
            enabled=True,
            interval=cfg.monitor_interval,
            on_claude_dead="restart",
            on_claude_degraded="kill_and_restart",
            degraded_threshold=3,
        )


# Type alias for the async alert callback
AlertFn = Callable[[str, str, str, str], Awaitable[None]]


class SelfMonitor:
    """Monitor claude processes in tmux windows and auto-recover.

    Runs as an asyncio task inside agentgate-backend's event loop. Checks all
    tmux windows periodically for claude health (alive/dead/degraded).

    Session recovery strategy:
      Priority 1: `--resume {session_id}` from session_map.json
      Priority 2: `--continue` (fallback when no session_map entry)

    Args:
        tmux_manager: TmuxManager instance for tmux operations.
        config: SelfMonitorConfig with thresholds and actions.
        alert_fn: Async callback(alert_type, severity, title, detail) for notifications.
        claude_command: Base command used to start claude in tmux panes.
    """

    def __init__(
        self,
        tmux_manager: TmuxManager,
        config: SelfMonitorConfig,
        alert_fn: AlertFn | None = None,
        claude_command: str | None = None,
    ) -> None:
        self._tmux = tmux_manager
        self._config = config
        self._alert_fn = alert_fn
        self._claude_command = claude_command or _backend_config.claude_command
        self._backoff = RestartBackoff(
            base_delay=_backend_config.restart_base_delay,
            max_delay=_backend_config.restart_max_delay,
            max_failures=_backend_config.restart_max_failures,
        )
        self._task: asyncio.Task | None = None

        # Per-window tracking
        self._consecutive_degraded: dict[str, int] = {}
        self._prev_alive: dict[str, bool | None] = {}
        self._window_statuses: dict[str, dict[str, Any]] = {}

    @property
    def window_statuses(self) -> dict[str, dict[str, Any]]:
        """Current health status per window — exposed for /api/health."""
        return dict(self._window_statuses)

    def _get_session_id(self, window_name: str) -> str | None:
        """Look up session_id for a window from session_map.json.

        Scans the session_map file for an entry whose window_name matches.
        Returns session_id string if found, None otherwise.
        """
        session_map_file = _backend_config.session_map_file
        if not session_map_file.exists():
            return None
        try:
            data = json.loads(session_map_file.read_text())
            prefix = f"{_backend_config.tmux_session_name}:"
            for key, info in data.items():
                if not key.startswith(prefix):
                    continue
                # Match by window_name field in info, or by window_id suffix
                if info.get("window_name") == window_name:
                    sid = info.get("session_id", "")
                    return sid if sid else None
            return None
        except (json.JSONDecodeError, OSError):
            return None

    def _get_session_id_by_window_id(self, window_id: str) -> str | None:
        """Look up session_id for a window_id from session_map.json.

        Key format in session_map: "{tmux_session_name}:{window_id}"
        Returns session_id string if found, None otherwise.
        """
        session_map_file = _backend_config.session_map_file
        if not session_map_file.exists():
            return None
        try:
            data = json.loads(session_map_file.read_text())
            key = f"{_backend_config.tmux_session_name}:{window_id}"
            info = data.get(key, {})
            sid = info.get("session_id", "")
            return sid if sid else None
        except (json.JSONDecodeError, OSError):
            return None

    def _build_restart_command(self, window_id: str, window_name: str) -> str:
        """Build the restart command for a window.

        Priority 1: `{claude_command} --resume {session_id}` from session_map.json
        Priority 2: `{claude_command} --continue` (fallback)

        This implements §4.5 of the tech design: use --resume when we know the
        session_id, so claude resumes exactly the right session rather than
        blindly picking the most-recently-modified JSONL in CWD.
        """
        base = self._claude_command

        # Try window_id first (most reliable), then window_name
        session_id = self._get_session_id_by_window_id(window_id)
        if not session_id:
            session_id = self._get_session_id(window_name)

        if session_id:
            logger.info(
                "SelfMonitor: using --resume %s for window %s (%s)",
                session_id, window_id, window_name,
            )
            return f"{base} --resume {session_id}"

        logger.info(
            "SelfMonitor: no session_id in session_map for window %s (%s), "
            "falling back to --continue",
            window_id, window_name,
        )
        return f"{base} --continue"

    async def check_all_windows(self) -> dict[str, dict[str, Any]]:
        """Check health of all tmux windows.

        Returns:
            {window_id: {status, window_name, detail, ...}}
            status is one of: ok, dead, degraded
        """
        results: dict[str, dict[str, Any]] = {}
        try:
            windows = await self._tmux.list_windows()
        except Exception as e:
            logger.error("SelfMonitor: failed to list windows: %s", e, exc_info=True)
            return results

        expected_process = _backend_config.process_name

        for w in windows:
            entry: dict[str, Any] = {
                "window_name": w.window_name,
                "pane_command": w.pane_current_command,
            }

            if w.pane_current_command != expected_process:
                entry["status"] = "dead"
                entry["detail"] = (
                    f"Window {w.window_name} running '{w.pane_current_command}' "
                    f"(expected '{expected_process}')"
                )
                results[w.window_id] = entry
                continue

            # Process is alive — check for degraded state via pane content
            entry["status"] = "ok"
            entry["detail"] = f"{expected_process} running in {w.window_name}"

            try:
                pane_text = await self._tmux.capture_pane(w.window_id)
                if pane_text:
                    lines = pane_text.strip().splitlines()
                    tail = lines[-10:] if len(lines) >= 10 else lines
                    tail_lower = "\n".join(tail).lower()

                    for pattern, label in _get_error_patterns():
                        if pattern in tail_lower:
                            entry["status"] = "degraded"
                            entry["degraded_reason"] = label
                            entry["pane_snippet"] = "\n".join(tail[-5:])
                            break
            except Exception as e:
                logger.warning("SelfMonitor: pane capture failed for %s: %s", w.window_id, e)

            results[w.window_id] = entry

        return results

    async def _send_alert(self, alert_type: str, severity: str, title: str, detail: str = "") -> None:
        """Send alert via configured callback, or log-only."""
        if self._alert_fn:
            try:
                await self._alert_fn(alert_type, severity, title, detail)
            except Exception as e:
                logger.error("SelfMonitor: alert_fn failed: %s", e, exc_info=True)
        else:
            log_msg = f"[SelfMonitor:{severity}] {title}"
            if detail:
                log_msg += f" | {detail}"
            if severity == "CRITICAL":
                logger.critical(log_msg)
            elif severity == "WARNING":
                logger.warning(log_msg)
            else:
                logger.info(log_msg)

    async def _restart_claude(self, wid: str, window_name: str = "") -> bool:
        """Restart claude in a tmux window using session-aware recovery command."""
        try:
            # Send an empty Enter to get a clean prompt, then the claude command
            await self._tmux.send_keys(wid, "", enter=True, literal=False)
            await asyncio.sleep(0.5)
            # Build restart command: --resume {session_id} or --continue
            restart_cmd = self._build_restart_command(wid, window_name)
            await self._tmux.send_keys(wid, restart_cmd, enter=True, literal=True)
            # Accept trust dialog if it appears
            await self._tmux._accept_trust_dialog(wid)
            logger.info("SelfMonitor: sent restart commands to window %s", wid)
            return True
        except Exception as e:
            logger.error("SelfMonitor: failed to restart claude in %s: %s", wid, e, exc_info=True)
            return False

    async def _verify_restart(self, wid: str, timeout: int = 30) -> bool:
        """Wait up to timeout seconds for claude to actually start in window."""
        expected_process = _backend_config.process_name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            try:
                windows = await self._tmux.list_windows()
                for w in windows:
                    if w.window_id == wid and w.pane_current_command == expected_process:
                        return True
            except Exception:
                pass
        return False

    async def _kill_claude(self, wid: str) -> bool:
        """Kill the claude process in a tmux window via pgrep + kill."""
        import subprocess as _sp

        try:
            # Get the pane's shell PID
            session = self._tmux.get_session()
            if not session:
                return False

            window = session.windows.get(window_id=wid)
            if not window:
                return False

            pane = window.active_pane
            if not pane:
                return False

            shell_pid = str(pane.pane_pid)

            # Find child process (claude) of the shell
            r = await asyncio.to_thread(
                _sp.run, ["pgrep", "-P", shell_pid],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return False

            child_pid = r.stdout.strip().splitlines()[0]
            await asyncio.to_thread(
                _sp.run, ["kill", child_pid], check=False, timeout=5,
            )
            logger.info("SelfMonitor: sent SIGTERM to PID %s (window %s)", child_pid, wid)
            return True
        except Exception as e:
            logger.error("SelfMonitor: failed to kill claude in %s: %s", wid, e, exc_info=True)
            return False

    async def _handle_dead(self, wid: str, info: dict[str, Any]) -> None:
        """Handle a dead claude process based on config.on_claude_dead."""
        action = self._config.on_claude_dead
        backoff_key = f"claude_restart:{wid}"

        if action == "ignore":
            return

        if action == "notify":
            await self._send_alert(
                "claude_process_dead", "CRITICAL",
                f"Claude 进程已死亡: {info.get('window_name', wid)}",
                info.get("detail", ""),
            )
            return

        # action is "restart" or "kill_and_restart"
        if self._backoff.is_circuit_open(backoff_key):
            await self._send_alert(
                "claude_process_dead", "CRITICAL",
                f"Claude 进程已死亡 — 自动重启熔断，需人工介入: {info.get('window_name', wid)}",
                info.get("detail", ""),
            )
            return

        if not self._backoff.may_restart(backoff_key):
            await self._send_alert(
                "claude_process_dead", "CRITICAL",
                f"Claude 进程已死亡 — 等待退避冷却: {info.get('window_name', wid)}",
                info.get("detail", ""),
            )
            return

        if action == "kill_and_restart":
            await self._kill_claude(wid)
            await asyncio.sleep(2)

        window_name = info.get("window_name", "")
        restarted = await self._restart_claude(wid, window_name)
        if restarted:
            verified = await self._verify_restart(wid)
            if verified:
                self._backoff.record_success(backoff_key)
                note = "已自动重启并验证成功"
            else:
                self._backoff.record_failure(backoff_key)
                note = "已发送重启命令但验证未通过"
        else:
            self._backoff.record_failure(backoff_key)
            note = "自动重启失败，请手动处理"

        severity = "INFO" if "验证成功" in note else "CRITICAL"
        await self._send_alert(
            "claude_process_dead", severity,
            f"Claude 进程已死亡 — {note}: {info.get('window_name', wid)}",
            info.get("detail", ""),
        )

    async def _handle_degraded(self, wid: str, info: dict[str, Any]) -> None:
        """Handle a degraded claude process (consecutive threshold exceeded)."""
        count = self._consecutive_degraded.get(wid, 0)
        if count < self._config.degraded_threshold:
            return  # Not enough consecutive detections yet

        action = self._config.on_claude_degraded
        reason = info.get("degraded_reason", "unknown")
        snippet = info.get("pane_snippet", "")
        backoff_key = f"claude_restart:{wid}"

        if action == "ignore":
            return

        if action == "notify":
            await self._send_alert(
                "claude_degraded", "CRITICAL",
                f"Claude API 异常: {reason} ({info.get('window_name', wid)})",
                f"连续 {count} 次检测到异常\n\nPane 末尾:\n{snippet}",
            )
            return

        if self._backoff.is_circuit_open(backoff_key):
            await self._send_alert(
                "claude_degraded", "CRITICAL",
                f"Claude API 异常: {reason} — 自动重启熔断: {info.get('window_name', wid)}",
                f"连续 {count} 次检测到异常\n\nPane 末尾:\n{snippet}",
            )
            return

        if not self._backoff.may_restart(backoff_key):
            await self._send_alert(
                "claude_degraded", "CRITICAL",
                f"Claude API 异常: {reason} — 等待退避冷却: {info.get('window_name', wid)}",
                f"连续 {count} 次检测到异常\n\nPane 末尾:\n{snippet}",
            )
            return

        # Execute action: kill (if needed) then restart, then verify
        if action in ("kill_and_restart", "restart"):
            if action == "kill_and_restart":
                await self._kill_claude(wid)
                await asyncio.sleep(2)

            window_name = info.get("window_name", "")
            restarted = await self._restart_claude(wid, window_name)
            if restarted:
                verified = await self._verify_restart(wid)
                if verified:
                    self._backoff.record_success(backoff_key)
                    note = "已重启并验证成功"
                else:
                    self._backoff.record_failure(backoff_key)
                    note = "已发送重启命令但验证未通过"
            else:
                self._backoff.record_failure(backoff_key)
                note = "重启失败"

            severity = "INFO" if "验证成功" in note else "CRITICAL"
            await self._send_alert(
                "claude_degraded", severity,
                f"Claude API 异常: {reason} — {note}: {info.get('window_name', wid)}",
                f"连续 {count} 次检测到异常\n\nPane 末尾:\n{snippet}",
            )

    async def _handle_recovery(self, wid: str, info: dict[str, Any]) -> None:
        """Handle recovery from dead or degraded state."""
        backoff_key = f"claude_restart:{wid}"
        self._backoff.record_success(backoff_key)
        self._consecutive_degraded[wid] = 0

        await self._send_alert(
            "claude_process_recovered", "INFO",
            f"Claude 进程已恢复: {info.get('window_name', wid)}",
            info.get("detail", ""),
        )

    async def _run_cycle(self) -> None:
        """Single monitoring cycle: check all windows and act."""
        statuses = await self.check_all_windows()
        self._window_statuses = statuses

        for wid, info in statuses.items():
            status = info["status"]
            prev_alive = self._prev_alive.get(wid)

            if status == "dead":
                await self._handle_dead(wid, info)
                self._prev_alive[wid] = False

            elif status == "degraded":
                self._consecutive_degraded[wid] = self._consecutive_degraded.get(wid, 0) + 1
                await self._handle_degraded(wid, info)
                self._prev_alive[wid] = True  # process is alive but degraded

            elif status == "ok":
                # Check for recovery from dead or degraded
                if prev_alive is False or self._consecutive_degraded.get(wid, 0) >= self._config.degraded_threshold:
                    await self._handle_recovery(wid, info)
                self._consecutive_degraded[wid] = 0
                self._prev_alive[wid] = True

    async def run(self) -> None:
        """Main monitoring loop — runs until cancelled."""
        logger.info(
            "SelfMonitor started (interval=%ds, on_dead=%s, on_degraded=%s)",
            self._config.interval,
            self._config.on_claude_dead,
            self._config.on_claude_degraded,
        )
        while True:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("SelfMonitor cycle error: %s", e, exc_info=True)
            await asyncio.sleep(self._config.interval)

    def start(self) -> None:
        """Start the monitoring loop as an asyncio task."""
        if self._task and not self._task.done():
            logger.warning("SelfMonitor: already running")
            return
        self._task = asyncio.create_task(self.run())
        logger.info("SelfMonitor task created")

    def stop(self) -> None:
        """Cancel the monitoring task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("SelfMonitor task cancelled")
        self._task = None
