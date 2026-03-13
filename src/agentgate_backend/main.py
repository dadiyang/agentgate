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
    from agentgate_backend.delivery_tracker import DeliveryTracker
    from agentgate_backend.heartbeat import write_heartbeat
    from agentgate_backend.inject_server import start_server
    from agentgate_backend.self_monitor import SelfMonitor, SelfMonitorConfig
    from agentgate_backend.session import SessionManager
    from agentgate_backend.tmux_manager import TmuxManager

    # 1. Init tmux manager
    tmux = TmuxManager(session_name=config.tmux_session_name)

    # 2. Init components
    tracker = DeliveryTracker()
    session_mgr = SessionManager()

    # 3. Start inject server (HTTP API)
    runner = await start_server(
        tracker=tracker,
        session_manager=session_mgr,
        tmux_manager=tmux,
        api_token=config.api_token,
        port=config.http_port,
    )

    # 4. Init self-monitor
    sm_config = SelfMonitorConfig.from_backend_config()
    monitor = SelfMonitor(
        tmux_manager=tmux,
        config=sm_config,
        claude_command=config.claude_command,
    )

    # 5. Heartbeat path
    heartbeat_path = (
        Path.home() / ".agentgate" / "heartbeat" / f"{config.name}.json"
    )

    logger.info("Backend '%s' started on port %d", config.name, config.http_port)

    # 6. Main loop — wait for SIGINT/SIGTERM
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

    # Graceful shutdown
    logger.info("Shutting down backend '%s'...", config.name)
    monitor.stop()
    monitor_task.cancel()
    heartbeat_task.cancel()
    await runner.cleanup()


if __name__ == "__main__":
    main()
