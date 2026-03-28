"""CLI entry point for AgentGate Gateway."""

import asyncio
import logging
import logging.handlers
import signal
from pathlib import Path

import click
from aiohttp import web

from agentgate_gateway.alert_manager import AlertManager
from agentgate_gateway.api import GatewayAPI, setup_routes
from agentgate_gateway.config import GatewayConfig
from agentgate_gateway.db import MessageDB
from agentgate_gateway.health_prober import BackendState, HealthProber
from agentgate_gateway.inbound_handler import InboundHandler
from agentgate_gateway.output_poller import OutputPoller
from agentgate_gateway.recovery import RecoveryManager
from agentgate_gateway.router import Router

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_LOG_DIR = Path.home() / ".agentgate" / "gateway" / "logs"
_LOG_MAX_BYTES = 100 * 1024 * 1024  # 100MB per file
_LOG_BACKUP_COUNT = 9  # 10 files × 100MB = 1GB total


def _setup_logging() -> None:
    """Configure logging with console + rotating file output (1GB cap)."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(_LOG_FORMAT)
    # Console
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    # Rotating file
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "gateway.log",
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


_setup_logging()
# Suppress httpx INFO logs (polling noise: ~95% of log volume).
# Errors/warnings still logged.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("agentgate-gateway")


@click.command()
@click.option("--config", "config_path", required=True, type=click.Path(exists=True))
@click.option("--port", type=int, default=None, help="Override config port")
@click.option("--test-mode", is_flag=True, default=False, help="Enable Admin API")
def main(config_path, port, test_mode):
    """Start the AgentGate Gateway."""
    config = GatewayConfig.from_yaml(Path(config_path))
    if port is not None:
        config.port = port
    if test_mode:
        config.test_mode = True
    asyncio.run(run(config, config_path=Path(config_path)))


async def run(config: GatewayConfig, config_path: Path | None = None) -> None:
    """Async main: wire up all components and start the gateway."""

    # 1. Init DB
    db = MessageDB(config.db_path)
    await db.init()

    # 2. Build router
    router = Router(config.routes)

    # 3. Build backend states
    backend_states: dict[str, BackendState] = {}
    for bid, bc in config.backends.items():
        backend_states[bid] = BackendState(
            url=bc.url, api_token=bc.api_token, default_window=bc.default_window
        )

    # 4. Adapters dict (populated below)
    adapters: dict = {}

    # 5. Alert manager (built early so inbound/outbound can use it)
    alert_mgr = AlertManager(config.alerts)

    # 6. Build inbound handler
    inbound = InboundHandler(db, router, backend_states, adapters, alert_manager=alert_mgr)

    # 7. Message callback for adapters
    async def on_message(
        channel_type: str,
        bot_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        group_name: str,
        text: str,
        dedup_key: str,
    ) -> None:
        await inbound.handle_message(
            channel_type, bot_id, chat_id, sender_id, sender_name, group_name, text, dedup_key
        )

    # 8. Create channel adapters
    if config.channels.feishu_apps:
        # Multi-app mode: each app gets its own adapter keyed by feishu:{app_id}
        from agentgate_gateway.adapters.feishu import FeishuAdapter

        for app_cfg in config.channels.feishu_apps:
            adapter = FeishuAdapter(
                app_cfg.app_id,
                app_cfg.app_secret,
                on_message,
            )
            adapters[f"feishu:{app_cfg.app_id}"] = adapter
    elif config.channels.feishu:
        # Single-app mode (backward compat): keyed as "feishu"
        from agentgate_gateway.adapters.feishu import FeishuAdapter

        adapters["feishu"] = FeishuAdapter(
            config.channels.feishu.app_id,
            config.channels.feishu.app_secret,
            on_message,
        )
    if config.channels.telegram_bots:
        # Multi-bot mode: each bot gets its own adapter keyed by telegram:{bot_id}
        from agentgate_gateway.adapters.telegram import TelegramAdapter

        for bot_cfg in config.channels.telegram_bots:
            adapter = TelegramAdapter(
                bot_cfg.bot_token,
                on_message,
                proxy=bot_cfg.proxy,
                bot_id_override=bot_cfg.bot_id,
                mention_only=bot_cfg.mention_only,
            )
            adapter_key = f"telegram:{bot_cfg.bot_id}" if bot_cfg.bot_id else f"telegram"
            adapters[adapter_key] = adapter
    elif config.channels.telegram:
        # Single-bot mode (backward compat): keyed as "telegram"
        from agentgate_gateway.adapters.telegram import TelegramAdapter

        adapters["telegram"] = TelegramAdapter(
            config.channels.telegram.bot_token,
            on_message,
            proxy=config.channels.telegram.proxy,
        )

    # 8. Output poller
    poller = OutputPoller(db, router, backend_states, adapters, config.poll_interval, alert_manager=alert_mgr)

    # 10. Recovery manager
    recovery = RecoveryManager(db, inbound.reinject_message, poller.repush_message)

    # 11. Health prober callbacks
    async def on_recovered(bid: str) -> None:
        # Reset output cursor for CC backends (JSONL file replaced on restart).
        # OC backends use SQLite timestamps that survive restarts — resetting
        # to 0 replays all history and floods IM.
        bc = config.backends.get(bid)
        if bc and getattr(bc, "agent_type", "claude-code") != "opencode":
            await poller.reset_offset(bid)
        await recovery.on_backend_recovered(bid)

    async def on_unhealthy(bid: str) -> None:
        await alert_mgr.send("backend_unhealthy", "CRITICAL", f"Backend {bid} unreachable", bid)

    prober = HealthProber(backend_states, on_recovered, on_unhealthy, config.probe_interval)

    # 12. Startup recovery (reinject pending messages from before crash)
    await recovery.recover_on_startup()

    # 13. HTTP API
    gateway_api = GatewayAPI(config, db, router, adapters, backend_states, inbound, poller, config_path=config_path)
    app = web.Application()
    setup_routes(app, gateway_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info("Gateway started on port %d (test_mode=%s)", config.port, config.test_mode)

    # 14. Start async tasks with adapter reconnect wrapper (E-4)
    async def adapter_run_loop(name: str, adapter, alert: AlertManager):
        """Outer reconnect loop: restart adapter on failure with exponential backoff.

        Never gives up — keeps retrying at max_delay interval after circuit opens.
        Resets failure counter after adapter runs successfully for >60s.
        """
        base_delay = 5
        max_delay = 300
        alert_threshold = 3
        critical_threshold = 10
        failures = 0
        alerted_warning = False
        alerted_critical = False
        while True:
            start_time = asyncio.get_event_loop().time()
            try:
                logger.info("Starting adapter %s (failures=%d)", name, failures)
                await adapter.start()
                # start() returned normally — adapter shut down cleanly
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # If adapter ran for >60s before failing, reset counter
                # (it was working, this is a new failure sequence)
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > 60:
                    failures = 0
                    alerted_warning = False
                    alerted_critical = False

                failures += 1
                delay = min(base_delay * (2 ** (failures - 1)), max_delay)
                logger.error(
                    "Adapter %s failed (attempt %d, ran %.0fs): %s — retrying in %ds",
                    name, failures, elapsed, e, delay, exc_info=True,
                )
                if failures == alert_threshold and not alerted_warning:
                    alerted_warning = True
                    await alert.send(
                        "channel_disconnect", "WARNING",
                        f"通道 {name} 连续 {failures} 次连接失败，持续重试中",
                        name,
                    )
                if failures == critical_threshold and not alerted_critical:
                    alerted_critical = True
                    await alert.send(
                        "channel_disconnect", "CRITICAL",
                        f"通道 {name} 连续 {critical_threshold} 次启动失败，继续重试中",
                        name,
                    )
                await asyncio.sleep(delay)

    tasks: list[asyncio.Task] = []
    for name, adapter in adapters.items():
        tasks.append(asyncio.create_task(adapter_run_loop(name, adapter, alert_mgr), name=f"adapter-{name}"))
    tasks.append(asyncio.create_task(poller.run(), name="output-poller"))
    tasks.append(asyncio.create_task(prober.run(), name="health-prober"))

    # 15. SIGHUP handler for config hot-reload (routes + backends)
    def handle_sighup():
        """Reload config.yaml on SIGHUP — delegates to GatewayAPI.reload_config()."""
        try:
            result = gateway_api.reload_config()
            if result["ok"]:
                logger.info("SIGHUP: reload ok — routes=%d, backends updated=%d added=%d",
                            result["routes"], result["backends_updated"], result["backends_added"])
            else:
                logger.warning("SIGHUP: reload rejected: %s", result.get("msg"))
        except Exception as e:
            logger.error("SIGHUP: reload failed: %s", e, exc_info=True)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGHUP, handle_sighup)

    # 16. Wait for shutdown signal
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # 16. Graceful shutdown
    logger.info("Shutting down...")
    poller.stop()
    prober.stop()
    for adapter in adapters.values():
        await adapter.stop()
    for task in tasks:
        task.cancel()
    await inbound.close()
    await poller.close()
    await prober.close()
    await runner.cleanup()
    await db.close()
    logger.info("Gateway shutdown complete.")


if __name__ == "__main__":
    main()
