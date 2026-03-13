"""Tests for agentgate-ctl CLI."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from agentgate_ctl.main import (
    _allocated_ports,
    _detect_bot_id,
    _next_port,
    _read_instance_env,
    cli,
)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Set up a temporary agentgate home directory."""
    agentgate_home = tmp_path / ".agentgate"
    gateway_dir = agentgate_home / "gateway"
    gateway_dir.mkdir(parents=True)
    backends_dir = agentgate_home / "backends"
    backends_dir.mkdir(parents=True)
    heartbeat_dir = agentgate_home / "heartbeat"
    heartbeat_dir.mkdir(parents=True)

    # Create a minimal gateway config
    config = {
        "channels": {
            "feishu": {"app_id": "test_app_id", "app_secret": "test_secret"},
            "telegram": {"bot_token": "123:ABC", "proxy": ""},
        },
        "backends": {
            "echo-a": {
                "url": "http://127.0.0.1:8950",
                "api_token": "echo-a-token",
                "agent_type": "echo",
            }
        },
        "routes": [
            {
                "channel": "feishu",
                "bot_id": "test_app_id",
                "group_id": "oc_group1",
                "backend": "echo-a",
            },
            {
                "channel": "telegram",
                "bot_id": "test_tg_bot",
                "group_id": "-123456",
                "backend": "echo-a",
            },
        ],
        "alerts": {},
    }
    config_path = gateway_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Patch module-level paths
    monkeypatch.setattr("agentgate_ctl.main.AGENTGATE_HOME", agentgate_home)
    monkeypatch.setattr("agentgate_ctl.main.GATEWAY_CONFIG", config_path)
    monkeypatch.setattr("agentgate_ctl.main.BACKENDS_DIR", backends_dir)
    monkeypatch.setattr("agentgate_ctl.main.HEARTBEAT_DIR", heartbeat_dir)

    return tmp_path


class TestPortAllocation:
    def test_allocated_ports_from_config(self, tmp_home):
        config = {
            "backends": {
                "a": {"url": "http://127.0.0.1:8903"},
                "b": {"url": "http://127.0.0.1:8905"},
            }
        }
        ports = _allocated_ports(config)
        assert 8903 in ports
        assert 8905 in ports
        assert 8904 not in ports

    def test_allocated_ports_from_env(self, tmp_home):
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "test-inst"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_PORT=8910\n")

        config = {"backends": {}}
        ports = _allocated_ports(config)
        assert 8910 in ports

    @patch("agentgate_ctl.main._port_in_use", return_value=False)
    def test_next_port_skips_used(self, mock_port, tmp_home):
        config = {
            "backends": {
                "a": {"url": "http://127.0.0.1:8903"},
                "b": {"url": "http://127.0.0.1:8904"},
            }
        }
        port = _next_port(config)
        assert port == 8905


class TestBotIdDetection:
    def test_feishu_bot_id(self):
        config = {"channels": {"feishu": {"app_id": "my_feishu_app"}}}
        assert _detect_bot_id(config, "feishu") == "my_feishu_app"

    def test_telegram_bot_id_from_routes(self):
        config = {
            "channels": {"telegram": {"bot_token": "123:ABC"}},
            "routes": [
                {"channel": "telegram", "bot_id": "my_tg_bot", "group_id": "-1"},
            ],
        }
        assert _detect_bot_id(config, "telegram") == "my_tg_bot"

    def test_unknown_channel(self):
        config = {"channels": {}}
        assert _detect_bot_id(config, "slack") == ""


class TestReadInstanceEnv:
    def test_reads_env_file(self, tmp_home):
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "my-inst"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text(
            "AGENTGATE_NAME=my-inst\nAGENTGATE_PORT=8910\n"
        )
        env = _read_instance_env("my-inst")
        assert env["AGENTGATE_NAME"] == "my-inst"
        assert env["AGENTGATE_PORT"] == "8910"

    def test_missing_env(self, tmp_home):
        env = _read_instance_env("nonexistent")
        assert env == {}


class TestCreateCommand:
    @patch("agentgate_ctl.main.subprocess.run")
    @patch("agentgate_ctl.main._systemctl")
    def test_create_basic(self, mock_systemctl, mock_subprocess, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        mock_subprocess.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "create", "test-new",
            "--channel", "feishu",
            "--group-id", "oc_newgroup",
        ])
        assert result.exit_code == 0, result.output
        assert "test-new" in result.output
        assert "created successfully" in result.output

        # Verify .env was created
        env_file = tmp_home / ".agentgate" / "backends" / "test-new" / ".env"
        assert env_file.exists()
        env_content = env_file.read_text()
        assert "AGENTGATE_NAME=test-new" in env_content

        # Verify gateway config was updated
        config_path = tmp_home / ".agentgate" / "gateway" / "config.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert "test-new" in config["backends"]
        # Check route was added
        backends_in_routes = [r["backend"] for r in config["routes"]]
        assert "test-new" in backends_in_routes

    @patch("agentgate_ctl.main.subprocess.run")
    @patch("agentgate_ctl.main._systemctl")
    def test_create_duplicate_name_fails(self, mock_systemctl, mock_subprocess, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        mock_subprocess.return_value = MagicMock(returncode=0)

        # Create first instance
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "existing"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_NAME=existing\n")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "create", "existing",
            "--channel", "feishu",
            "--group-id", "oc_group2",
        ])
        assert result.exit_code != 0
        assert "already exists" in result.output

    @patch("agentgate_ctl.main._port_in_use", return_value=False)
    @patch("agentgate_ctl.main.subprocess.run")
    @patch("agentgate_ctl.main._systemctl")
    def test_create_port_auto_allocation(self, mock_systemctl, mock_subprocess, mock_port, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        mock_subprocess.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "create", "inst1",
            "--channel", "feishu",
            "--group-id", "oc_g1",
        ])
        assert result.exit_code == 0

        # Port should be 8903 (first available, 8950 is echo-a)
        env_file = tmp_home / ".agentgate" / "backends" / "inst1" / ".env"
        content = env_file.read_text()
        assert "AGENTGATE_PORT=8903" in content


class TestListCommand:
    def test_list_empty(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No instances found" in result.output

    def test_list_with_instances(self, tmp_home):
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "my-agent"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text(
            "AGENTGATE_NAME=my-agent\nAGENTGATE_PORT=8903\n"
        )

        runner = CliRunner()
        with patch("agentgate_ctl.main._is_service_active", return_value=False):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "my-agent" in result.output
        assert "8903" in result.output


class TestStatusCommand:
    def test_status_nonexistent(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_status_existing(self, tmp_home):
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "my-agent"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text(
            "AGENTGATE_NAME=my-agent\nAGENTGATE_PORT=8903\n"
            "AGENTGATE_WORK_DIR=/tmp/test\nAGENTGATE_CLAUDE_COMMAND=claude\n"
        )

        runner = CliRunner()
        with patch("agentgate_ctl.main._is_service_active", return_value=True):
            result = runner.invoke(cli, ["status", "my-agent"])
        assert result.exit_code == 0
        assert "my-agent" in result.output
        assert "active" in result.output


class TestRemoveCommand:
    @patch("agentgate_ctl.main._systemctl")
    def test_remove_with_confirm(self, mock_systemctl, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)

        # Create instance files
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "to-remove"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_NAME=to-remove\n")

        # Add to gateway config
        config_path = tmp_home / ".agentgate" / "gateway" / "config.yaml"
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["backends"]["to-remove"] = {
            "url": "http://127.0.0.1:8910",
            "api_token": "tok",
        }
        config["routes"].append({
            "channel": "feishu",
            "bot_id": "test_app_id",
            "group_id": "oc_remove",
            "backend": "to-remove",
        })
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        runner = CliRunner()
        result = runner.invoke(cli, ["remove", "to-remove", "-y"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

        # Verify removed from config
        with open(config_path) as f:
            config = yaml.safe_load(f)
        assert "to-remove" not in config.get("backends", {})
        assert all(r["backend"] != "to-remove" for r in config.get("routes", []))


class TestStopStartRestartCommands:
    @patch("agentgate_ctl.main._systemctl")
    def test_stop(self, mock_systemctl, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "inst1"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_NAME=inst1\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "inst1"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    @patch("agentgate_ctl.main._systemctl")
    def test_start(self, mock_systemctl, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "inst1"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_NAME=inst1\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["start", "inst1"])
        assert result.exit_code == 0
        assert "started" in result.output.lower()

    @patch("agentgate_ctl.main._systemctl")
    def test_restart(self, mock_systemctl, tmp_home):
        mock_systemctl.return_value = MagicMock(returncode=0)
        backends_dir = tmp_home / ".agentgate" / "backends"
        inst_dir = backends_dir / "inst1"
        inst_dir.mkdir()
        (inst_dir / ".env").write_text("AGENTGATE_NAME=inst1\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["restart", "inst1"])
        assert result.exit_code == 0
        assert "restarted" in result.output.lower()

    def test_stop_nonexistent(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output
