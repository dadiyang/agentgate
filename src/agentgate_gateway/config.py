from pathlib import Path

import yaml
from pydantic import BaseModel


class FeishuConfig(BaseModel):
    """Single feishu app config (backward compat)."""
    app_id: str
    app_secret: str


class FeishuAppConfig(BaseModel):
    app_id: str
    app_secret: str


class TelegramBotConfig(BaseModel):
    bot_token: str
    bot_id: str = ""  # Optional: auto-detected from get_me() if empty
    proxy: str = ""
    mention_only: bool = False  # If True, only process messages that @mention this bot


class TelegramConfig(BaseModel):
    """Single bot config (backward compat)."""
    bot_token: str
    proxy: str = ""


class DingTalkBotConfig(BaseModel):
    client_id: str
    client_secret: str
    bot_id: str = ""          # Optional: used as bot_id in routes, defaults to client_id
    allow_from: str = "*"     # "*" or comma-separated staffId whitelist


class ChannelsConfig(BaseModel):
    feishu: FeishuConfig | None = None
    feishu_apps: list[FeishuAppConfig] | None = None
    telegram: TelegramConfig | None = None
    telegram_bots: list[TelegramBotConfig] | None = None
    dingtalk_bots: list[DingTalkBotConfig] | None = None


class BackendConfig(BaseModel):
    url: str
    api_token: str
    agent_type: str = "claude-code"
    default_window: str = "main"


class RouteConfig(BaseModel):
    channel: str
    bot_id: str
    chat_id: str
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

        channels = data.get("channels", {})

        # Multi-app feishu support: if channels.feishu has an "apps" key,
        # parse as list of FeishuAppConfig and move to feishu_apps field.
        feishu = channels.get("feishu")
        if isinstance(feishu, dict) and "apps" in feishu:
            apps_data = feishu.pop("apps")
            channels["feishu_apps"] = apps_data
            if not feishu:  # empty after extracting apps
                del channels["feishu"]

        # Multi-bot telegram support: if channels.telegram has a "bots" key,
        tg = channels.get("telegram")
        if isinstance(tg, dict) and "bots" in tg:
            bots_data = tg.pop("bots")
            # Inherit top-level proxy as default for each bot
            default_proxy = tg.pop("proxy", "")
            for b in bots_data:
                if not b.get("proxy") and default_proxy:
                    b["proxy"] = default_proxy
            channels["telegram_bots"] = bots_data
            # Remove the original telegram key (no longer a single-bot config)
            if not tg:  # empty after extracting bots and proxy
                del channels["telegram"]

        # DingTalk: normalize channels.dingtalk.bots → dingtalk_bots
        dt = channels.get("dingtalk")
        if isinstance(dt, dict) and "bots" in dt:
            channels["dingtalk_bots"] = dt.pop("bots")
            if not dt:
                del channels["dingtalk"]

        return cls(**data)
