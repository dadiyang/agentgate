"""Backend instance configuration.

Provides a BackendConfig pydantic model and a module-level `config` reference
that forked ccbot modules can import. The config instance is set at startup
via `init_config()`.
"""

import logging
from pathlib import Path

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class BackendConfig(BaseSettings):
    """agentgate-backend instance configuration."""

    name: str = "default"
    port: int = 8901
    api_token: str = ""
    work_dir: Path = Path.home()
    tmux_session_name: str = "agentgate"
    tmux_main_window_name: str = "__main__"
    initial_window_name: str = ""  # Default: work_dir basename
    claude_command: str = "claude"
    process_name: str = "claude"

    # SelfMonitor
    monitor_interval: int = 30
    restart_base_delay: int = 5
    restart_max_delay: int = 300
    restart_max_failures: int = 10

    # Paths
    data_dir: Path = Path.home() / ".agentgate" / "backends"

    # Claude Code session monitoring
    claude_projects_path: Path = Path.home() / ".claude" / "projects"
    monitor_poll_interval: float = 2.0

    # HTTP API
    http_port: int = 8901

    # Watchdog
    watchdog_alert_chat_id: str = ""

    class Config:
        env_prefix = "AGENTGATE_"
        env_file = ".env"

    @property
    def instance_dir(self) -> Path:
        return self.data_dir / self.name

    @property
    def config_dir(self) -> Path:
        return self.instance_dir

    @property
    def state_file(self) -> Path:
        return self.instance_dir / "state.json"

    @property
    def session_map_file(self) -> Path:
        return self.instance_dir / "session_map.json"

    @property
    def monitor_state_file(self) -> Path:
        return self.instance_dir / "monitor_state.json"


# Module-level config reference — set by init_config() at startup.
# Forked ccbot modules import this via `from .config import config`.
config: BackendConfig = BackendConfig()


def init_config(**kwargs) -> BackendConfig:
    """Initialize the global config. Called once at startup."""
    global config
    config = BackendConfig(**kwargs)
    config.instance_dir.mkdir(parents=True, exist_ok=True)
    return config
