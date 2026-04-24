"""Tests for agentgate-ctl switch and reroute commands."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from agentgate_ctl.main import cli


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolated agentgate home with two backends and one route on echo-a."""
    ag_home = tmp_path / ".agentgate"
    gw_dir = ag_home / "gateway"
    gw_dir.mkdir(parents=True)
    backends_dir = ag_home / "backends"
    backends_dir.mkdir(parents=True)
    (ag_home / "heartbeat").mkdir(parents=True)

    config = {
        "port": 8800,
        "backends": {
            "echo-a": {
                "url": "http://127.0.0.1:8950",
                "api_token": "echo-a-token",
                "agent_type": "claude-code",
                "default_window": "proj-alpha",
            },
            "echo-b": {
                "url": "http://127.0.0.1:8951",
                "api_token": "echo-b-token",
                "agent_type": "claude-code",
                "default_window": "proj-beta",
            },
        },
        "routes": [
            {
                "channel": "telegram",
                "bot_id": "mybot",
                "chat_id": "-100123",
                "backend": "echo-a",
            }
        ],
    }
    config_path = gw_dir / "config.yaml"
    config_path.write_text(yaml.dump(config))

    # Create .env files for existing backends
    for name, bc in config["backends"].items():
        inst = backends_dir / name
        inst.mkdir()
        (inst / ".env").write_text(
            f"AGENTGATE_NAME={name}\nAGENTGATE_PORT={bc['url'].rsplit(':', 1)[-1]}\n"
        )

    monkeypatch.setattr("agentgate_ctl.main.AGENTGATE_HOME", ag_home)
    monkeypatch.setattr("agentgate_ctl.main.GATEWAY_CONFIG", config_path)
    monkeypatch.setattr("agentgate_ctl.main.BACKENDS_DIR", backends_dir)
    monkeypatch.setattr("agentgate_ctl.main.HEARTBEAT_DIR", ag_home / "heartbeat")

    return tmp_path


# ─────────────────────────── switch --dry-run ────────────────────────────────

class TestSwitchDryRun:
    def test_dry_run_shows_plan_without_changes(self, tmp_home):
        runner = CliRunner()
        config_path = tmp_home / ".agentgate" / "gateway" / "config.yaml"
        original = config_path.read_text()

        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/test-ws",
            "--dry-run",
        ])

        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert "echo-a-new" in result.output
        assert "/tmp/test-ws" in result.output
        assert "test-ws" in result.output          # default_window = basename
        assert "echo-a → echo-a-new" in result.output
        assert "telegram/mybot/-100123" in result.output

        # Config must be unchanged
        assert config_path.read_text() == original
        # No .env created
        assert not (tmp_home / ".agentgate" / "backends" / "echo-a-new").exists()

    def test_dry_run_shows_keep_old(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/test-ws",
            "--keep-old",
            "--dry-run",
        ])
        assert result.exit_code == 0
        assert "REMOVE" not in result.output

    def test_dry_run_custom_new_name(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/test-ws",
            "--new-name", "echo-a-v2",
            "--dry-run",
        ])
        assert result.exit_code == 0
        assert "echo-a-v2" in result.output


# ─────────────────────────── switch validation ───────────────────────────────

class TestSwitchValidation:
    def test_old_backend_not_found(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "nonexistent",
            "--workdir", "/tmp/ws",
            "--dry-run",
        ])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_new_name_collision(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/ws",
            "--new-name", "echo-b",   # already exists
            "--dry-run",
        ])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_auto_name_avoids_collision(self, tmp_home):
        """If <old>-new already exists, auto-name should try <old>-new2."""
        backends_dir = tmp_home / ".agentgate" / "backends"
        collision = backends_dir / "echo-a-new"
        collision.mkdir()
        (collision / ".env").write_text("AGENTGATE_NAME=echo-a-new\n")

        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/ws",
            "--dry-run",
        ])
        assert result.exit_code == 0
        assert "echo-a-new2" in result.output


# ─────────────────────────── switch execution ────────────────────────────────

class TestSwitchExecution:
    @patch("agentgate_ctl.main._port_in_use", return_value=False)
    @patch("agentgate_ctl.main._clean_db_offset")
    @patch("agentgate_ctl.main._reset_backend_offset")
    @patch("agentgate_ctl.main._reload_gateway")
    @patch("agentgate_ctl.main._systemctl")
    @patch("agentgate_ctl.main.subprocess.run")
    def test_switch_creates_env_and_reroutes(
        self, mock_run, mock_systemctl, mock_reload, mock_reset, mock_clean, mock_port,
        tmp_home,
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_systemctl.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/new-ws",
            "--yes",
        ])
        assert result.exit_code == 0, result.output

        config_path = tmp_home / ".agentgate" / "gateway" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())

        # New backend present in config
        assert "echo-a-new" in config["backends"]
        new_bc = config["backends"]["echo-a-new"]
        assert new_bc["default_window"] == "new-ws"
        assert new_bc["agent_type"] == "claude-code"

        # Route rerouted to new backend
        backends_in_routes = [r["backend"] for r in config["routes"]]
        assert "echo-a-new" in backends_in_routes
        assert "echo-a" not in backends_in_routes

        # Old backend removed from config (not --keep-old)
        assert "echo-a" not in config["backends"]

        # .env created for new backend
        env_file = tmp_home / ".agentgate" / "backends" / "echo-a-new" / ".env"
        assert env_file.exists()
        env_text = env_file.read_text()
        assert "AGENTGATE_NAME=echo-a-new" in env_text
        assert "AGENTGATE_WORK_DIR=/tmp/new-ws" in env_text
        assert "new-ws" in env_text  # default_window in .env path

        # Gateway reload called at least twice (once after create, once after remove)
        assert mock_reload.call_count >= 2
        # Offset reset called for new backend
        mock_reset.assert_any_call("echo-a-new")
        # DB cleanup called for old backend
        mock_clean.assert_called_with("echo-a")

    @patch("agentgate_ctl.main._port_in_use", return_value=False)
    @patch("agentgate_ctl.main._clean_db_offset")
    @patch("agentgate_ctl.main._reset_backend_offset")
    @patch("agentgate_ctl.main._reload_gateway")
    @patch("agentgate_ctl.main._systemctl")
    @patch("agentgate_ctl.main.subprocess.run")
    def test_switch_keep_old_preserves_old_backend(
        self, mock_run, mock_systemctl, mock_reload, mock_reset, mock_clean, mock_port,
        tmp_home,
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_systemctl.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/new-ws",
            "--keep-old",
            "--yes",
        ])
        assert result.exit_code == 0, result.output

        config = yaml.safe_load(
            (tmp_home / ".agentgate" / "gateway" / "config.yaml").read_text()
        )
        # Old backend still in config
        assert "echo-a" in config["backends"]
        # DB cleanup NOT called
        mock_clean.assert_not_called()

    @patch("agentgate_ctl.main._port_in_use", return_value=False)
    @patch("agentgate_ctl.main._clean_db_offset")
    @patch("agentgate_ctl.main._reset_backend_offset")
    @patch("agentgate_ctl.main._reload_gateway")
    @patch("agentgate_ctl.main._systemctl")
    @patch("agentgate_ctl.main.subprocess.run")
    def test_switch_inherits_agent_type(
        self, mock_run, mock_systemctl, mock_reload, mock_reset, mock_clean, mock_port,
        tmp_home,
    ):
        """New backend should inherit agent_type from old backend."""
        # Change echo-a to opencode in fixture config
        config_path = tmp_home / ".agentgate" / "gateway" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["backends"]["echo-a"]["agent_type"] = "opencode"
        config_path.write_text(yaml.dump(config))

        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_systemctl.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(cli, [
            "switch", "echo-a",
            "--workdir", "/tmp/new-ws",
            "--yes",
        ])
        assert result.exit_code == 0

        new_config = yaml.safe_load(config_path.read_text())
        assert new_config["backends"]["echo-a-new"]["agent_type"] == "opencode"

        env_text = (
            tmp_home / ".agentgate" / "backends" / "echo-a-new" / ".env"
        ).read_text()
        assert "AGENTGATE_AGENT_TYPE=opencode" in env_text


# ─────────────────────────── reroute ─────────────────────────────────────────

class TestReroute:
    def test_dry_run_shows_route_change(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["reroute", "echo-a", "echo-b", "--dry-run"])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert "echo-a → echo-b" in result.output
        assert "telegram/mybot/-100123" in result.output

        # Config unchanged
        config = yaml.safe_load(
            (tmp_home / ".agentgate" / "gateway" / "config.yaml").read_text()
        )
        assert config["routes"][0]["backend"] == "echo-a"

    def test_from_backend_not_found(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["reroute", "missing", "echo-b"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_to_backend_not_found(self, tmp_home):
        runner = CliRunner()
        result = runner.invoke(cli, ["reroute", "echo-a", "missing"])
        assert result.exit_code != 0
        assert "not found" in result.output

    @patch("agentgate_ctl.main._reset_backend_offset")
    @patch("agentgate_ctl.main._reload_gateway")
    def test_reroute_updates_config_and_reloads(
        self, mock_reload, mock_reset, tmp_home
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["reroute", "echo-a", "echo-b"])
        assert result.exit_code == 0, result.output

        config = yaml.safe_load(
            (tmp_home / ".agentgate" / "gateway" / "config.yaml").read_text()
        )
        backends_in_routes = [r["backend"] for r in config["routes"]]
        assert "echo-b" in backends_in_routes
        assert "echo-a" not in backends_in_routes

        mock_reload.assert_called_once()
        mock_reset.assert_called_once_with("echo-b")

    @patch("agentgate_ctl.main._reset_backend_offset")
    @patch("agentgate_ctl.main._reload_gateway")
    def test_reroute_warns_when_no_routes(self, mock_reload, mock_reset, tmp_home):
        """Rerouting from a backend with no routes should warn but succeed."""
        runner = CliRunner()
        result = runner.invoke(cli, ["reroute", "echo-b", "echo-a"])
        assert result.exit_code == 0
        assert "no routes" in result.output.lower() or "0 route" in result.output
