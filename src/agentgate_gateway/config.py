from pathlib import Path

import yaml
from pydantic import BaseModel


class FeishuConfig(BaseModel):
    app_id: str
    app_secret: str


class TelegramConfig(BaseModel):
    bot_token: str
    proxy: str = ""


class ChannelsConfig(BaseModel):
    feishu: FeishuConfig | None = None
    telegram: TelegramConfig | None = None


class BackendConfig(BaseModel):
    url: str
    api_token: str
    agent_type: str = "claude-code"
    default_window: str = "main"


class RouteConfig(BaseModel):
    channel: str
    bot_id: str
    group_id: str
    backend: str


class AlertsConfig(BaseModel):
    feishu_webhook: str = ""
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""
    telegram_proxy: str = ""


class GatewayConfig(BaseModel):
    channels: ChannelsConfig = ChannelsConfig()
    backends: dict[str, BackendConfig] = {}
    routes: list[RouteConfig] = []
    alerts: AlertsConfig = AlertsConfig()
    api_token: str = ""
    port: int = 8800
    db_path: Path = Path.home() / ".agentgate" / "gateway" / "messages.db"
    poll_interval: float = 2.0
    probe_interval: float = 30.0
    test_mode: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> "GatewayConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
