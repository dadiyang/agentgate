"""Tests for agentgate_gateway.config"""

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgate_gateway.config import GatewayConfig


def write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "gateway.yaml"
    p.write_text(textwrap.dedent(content))
    return p


class TestLoadValidYaml:
    def test_full_config(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            port: 9000
            poll_interval: 5.0
            probe_interval: 60.0
            test_mode: true
            channels:
              feishu:
                app_id: cli_abc123
                app_secret: secret_feishu
              telegram:
                bot_token: 1234567:AABBcc
                proxy: http://127.0.0.1:7890
            backends:
              fish-dev:
                url: http://localhost:8001
                api_token: tok_dev
                agent_type: claude-code
            routes:
              - channel: feishu
                bot_id: cli_abc123
                chat_id: grp_001
                backend: fish-dev
            alerts:
              feishu_webhook: https://open.feishu.cn/webhook/xxx
              telegram_chat_id: "-100123"
              telegram_bot_token: 9876:ZZZtg
            """,
        )
        cfg = GatewayConfig.from_yaml(yaml_path)

        assert cfg.port == 9000
        assert cfg.poll_interval == 5.0
        assert cfg.probe_interval == 60.0
        assert cfg.test_mode is True

        assert cfg.channels.feishu is not None
        assert cfg.channels.feishu.app_id == "cli_abc123"
        assert cfg.channels.feishu.app_secret == "secret_feishu"

        assert cfg.channels.telegram is not None
        assert cfg.channels.telegram.bot_token == "1234567:AABBcc"
        assert cfg.channels.telegram.proxy == "http://127.0.0.1:7890"

        assert "fish-dev" in cfg.backends
        backend = cfg.backends["fish-dev"]
        assert backend.url == "http://localhost:8001"
        assert backend.api_token == "tok_dev"
        assert backend.agent_type == "claude-code"

        assert len(cfg.routes) == 1
        route = cfg.routes[0]
        assert route.channel == "feishu"
        assert route.bot_id == "cli_abc123"
        assert route.chat_id == "grp_001"
        assert route.backend == "fish-dev"

        assert cfg.alerts.feishu_webhook == "https://open.feishu.cn/webhook/xxx"
        assert cfg.alerts.telegram_chat_id == "-100123"


class TestDefaults:
    def test_minimal_yaml_uses_defaults(self, tmp_path):
        yaml_path = write_yaml(tmp_path, "port: 8800\n")
        cfg = GatewayConfig.from_yaml(yaml_path)

        assert cfg.port == 8800
        assert cfg.poll_interval == 2.0
        assert cfg.probe_interval == 30.0
        assert cfg.test_mode is False
        assert cfg.channels.feishu is None
        assert cfg.channels.telegram is None
        assert cfg.backends == {}
        assert cfg.routes == []
        assert cfg.alerts.feishu_webhook == ""
        assert cfg.alerts.telegram_chat_id == ""
        assert cfg.alerts.telegram_bot_token == ""

    def test_default_db_path(self, tmp_path):
        yaml_path = write_yaml(tmp_path, "{}\n")
        cfg = GatewayConfig.from_yaml(yaml_path)
        assert cfg.db_path == Path.home() / ".agentgate" / "gateway" / "messages.db"

    def test_telegram_proxy_default_empty(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            channels:
              telegram:
                bot_token: abc:123
            """,
        )
        cfg = GatewayConfig.from_yaml(yaml_path)
        assert cfg.channels.telegram is not None
        assert cfg.channels.telegram.proxy == ""

    def test_backend_agent_type_default(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            backends:
              dev:
                url: http://localhost:8001
                api_token: tok
            """,
        )
        cfg = GatewayConfig.from_yaml(yaml_path)
        assert cfg.backends["dev"].agent_type == "claude-code"


class TestInvalidConfig:
    def test_feishu_missing_required_field_raises(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            channels:
              feishu:
                app_id: cli_abc
            """,
        )
        with pytest.raises(ValidationError):
            GatewayConfig.from_yaml(yaml_path)

    def test_backend_missing_url_raises(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            backends:
              dev:
                api_token: tok
            """,
        )
        with pytest.raises(ValidationError):
            GatewayConfig.from_yaml(yaml_path)

    def test_route_missing_backend_raises(self, tmp_path):
        yaml_path = write_yaml(
            tmp_path,
            """\
            routes:
              - channel: feishu
                bot_id: bot1
                group_id: grp1
            """,
        )
        with pytest.raises(ValidationError):
            GatewayConfig.from_yaml(yaml_path)
