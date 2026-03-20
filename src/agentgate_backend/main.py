"""agentgate-backend CLI entry point."""

import asyncio
import logging
import signal
from pathlib import Path

import click

from agentgate_backend.config import BackendConfig, init_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("agentgate-backend")


@click.command()
@click.option("--name", required=True, help="Instance name (e.g., fish-dev)")
@click.option("--port", type=int, default=None, help="HTTP API port")
@click.option(
    "--work-dir",
    type=click.Path(exists=True),
    default=None,
    help="Project working directory",
)
def main(name: str, port: int | None, work_dir: str | None) -> None:
    """Start an agentgate-backend instance."""
    kwargs: dict = {"name": name}
    if port is not None:
        kwargs["port"] = port
        kwargs["http_port"] = port
    if work_dir is not None:
        kwargs["work_dir"] = work_dir
    config = init_config(**kwargs)
    asyncio.run(run(config))


async def run(config: BackendConfig) -> None:
    """Unified startup — creates the appropriate driver, then runs generic loop."""
    from agentgate_backend.delivery_tracker import DeliveryTracker
    from agentgate_backend.heartbeat import write_heartbeat
    from agentgate_backend.inject_server import start_server_with_driver
    from agentgate_backend.self_monitor import SelfMonitor, SelfMonitorConfig
    from agentgate_backend.tmux_manager import TmuxManager

    tmux = TmuxManager(session_name=config.tmux_session_name)
    tracker = DeliveryTracker()

    # Create driver based on agent_type
    driver = _create_driver(config, tmux)

    # Start HTTP server (uses AgentDriver for inject + output)
    runner = await start_server_with_driver(
        driver=driver,
        tracker=tracker,
        api_token=config.api_token,
        port=config.http_port,
    )

    # Self-monitor (uses AgentDriver for process_name, error_patterns, recovery)
    sm_config = SelfMonitorConfig.from_backend_config()
    monitor = SelfMonitor(
        tmux_manager=tmux,
        config=sm_config,
        claude_command=driver.get_start_command(str(config.work_dir)),
        agent_driver=driver,
    )

    heartbeat_path = Path.home() / ".agentgate" / "heartbeat" / f"{config.name}.json"

    logger.info(
        "Backend '%s' started on port %d (agent_type=%s)",
        config.name, config.http_port, config.agent_type,
    )

    # Auto-create initial window if work_dir is configured
    work_dir = config.work_dir
    if work_dir != Path.home():
        existing_windows = await tmux.list_windows()
        if not existing_windows:
            start_cmd = driver.get_start_command(str(work_dir))
            logger.info("No windows — bootstrapping %s in %s", config.agent_type, work_dir)
            default_window = config.initial_window_name or work_dir.name
            ok, msg, wname, wid = await tmux.create_window(
                work_dir=str(work_dir),
                window_name=default_window,
                start_claude=True,
                claude_command_override=start_cmd,
            )
            if ok:
                logger.info("Initial window created: %s (id=%s)", wname, wid)
                await driver.accept_startup_prompts(wid)
            else:
                logger.error("Failed to create initial window: %s", msg)

    # Main loop
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def heartbeat_loop() -> None:
        while not stop.is_set():
            write_heartbeat(heartbeat_path)
            await asyncio.sleep(30)

    monitor_task = asyncio.create_task(monitor.run())
    heartbeat_task = asyncio.create_task(heartbeat_loop())

    await stop.wait()

    logger.info("Shutting down backend '%s'...", config.name)
    monitor.stop()
    monitor_task.cancel()
    heartbeat_task.cancel()
    if hasattr(driver, "close"):
        driver.close()
    await runner.cleanup()


def _create_driver(config: BackendConfig, tmux):
    """Create AgentDriver based on config.agent_type + config.agent_mode.

    Matrix:
      agent_type=claude-code + agent_mode=tmux       → ClaudeCodeDriver (tmux + JSONL)
      agent_type=claude-code + agent_mode=subprocess  → ClaudeCodeSubprocessDriver (stream-json)
      agent_type=opencode    + agent_mode=tmux        → OpenCodeTmuxDriver (tmux + SQLite)
      agent_type=opencode    + agent_mode=subprocess  → OpenCodeSubprocessDriver (run per-turn)
    """
    agent_type = config.agent_type
    agent_mode = config.agent_mode

    if agent_type == "opencode" and agent_mode == "subprocess":
        from agentgate_backend.opencode_subprocess_driver import OpenCodeSubprocessDriver
        opencode_cmd = config.claude_command if config.claude_command != "claude" else "opencode"
        return OpenCodeSubprocessDriver(
            work_dir=str(config.work_dir),
            opencode_cmd=opencode_cmd,
            model=config.opencode_model,
        )

    if agent_type == "opencode":  # tmux mode
        from agentgate_backend.opencode_tmux_driver import OpenCodeTmuxDriver
        opencode_cmd = config.claude_command if config.claude_command != "claude" else "opencode"
        return OpenCodeTmuxDriver(
            tmux_manager=tmux,
            opencode_command=opencode_cmd,
            model=config.opencode_model,
            work_dir=str(config.work_dir),
        )

    if agent_type == "claude-code" and agent_mode == "subprocess":
        from agentgate_backend.claude_code_subprocess_driver import ClaudeCodeSubprocessDriver
        return ClaudeCodeSubprocessDriver(
            claude_command=config.claude_command,
            work_dir=str(config.work_dir),
        )

    # Default: claude-code + tmux
    from agentgate_backend.claude_code_driver import ClaudeCodeDriver
    from agentgate_backend.session import SessionManager
    return ClaudeCodeDriver(
        claude_command=config.claude_command,
        session_manager=SessionManager(),
        tmux_manager=tmux,
    )


if __name__ == "__main__":
    main()
