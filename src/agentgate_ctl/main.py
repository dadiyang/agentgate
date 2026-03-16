"""agentgate-ctl: management CLI for AgentGate backend instances."""

import json
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import click
import yaml

AGENTGATE_HOME = Path.home() / ".agentgate"
GATEWAY_CONFIG = AGENTGATE_HOME / "gateway" / "config.yaml"
BACKENDS_DIR = AGENTGATE_HOME / "backends"
HEARTBEAT_DIR = AGENTGATE_HOME / "heartbeat"
SYSTEMD_TEMPLATE = "agentgate-backend@{name}.service"
PORT_RANGE_START = 8903
PORT_RANGE_END = 8999


def _load_gateway_config() -> dict:
    if not GATEWAY_CONFIG.exists():
        click.echo(f"Error: gateway config not found at {GATEWAY_CONFIG}", err=True)
        sys.exit(1)
    with open(GATEWAY_CONFIG) as f:
        return yaml.safe_load(f) or {}


def _save_gateway_config(data: dict) -> None:
    GATEWAY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(GATEWAY_CONFIG, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _allocated_ports(config: dict) -> set[int]:
    """Collect all ports already used by backends in gateway config."""
    ports = set()
    for bid, bc in config.get("backends", {}).items():
        url = bc.get("url", "")
        # url format: http://127.0.0.1:<port>
        try:
            port = int(url.rsplit(":", 1)[-1])
            ports.add(port)
        except (ValueError, IndexError):
            pass
    # Also check .env files for ports not yet in gateway config
    if BACKENDS_DIR.exists():
        for env_file in BACKENDS_DIR.glob("*/.env"):
            for line in env_file.read_text().splitlines():
                if line.startswith("AGENTGATE_PORT="):
                    try:
                        ports.add(int(line.split("=", 1)[1]))
                    except ValueError:
                        pass
    return ports


def _port_in_use(port: int) -> bool:
    """Check if a port is actually in use on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _next_port(config: dict) -> int:
    used = _allocated_ports(config)
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in used and not _port_in_use(port):
            return port
    click.echo("Error: no available ports in range 8903-8999", err=True)
    sys.exit(1)


def _detect_bot_id(config: dict, channel: str) -> str:
    """Auto-detect bot_id from gateway channel config."""
    channels = config.get("channels", {})
    if channel == "feishu":
        feishu = channels.get("feishu", {})
        return feishu.get("app_id", "")
    elif channel == "telegram":
        # Look at existing telegram routes for bot_id
        for route in config.get("routes", []):
            if route.get("channel") == "telegram":
                return route.get("bot_id", "")
        # Fallback: extract from bot token (not reliable, use placeholder)
        return ""
    return ""


def _systemctl(action: str, service: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "systemctl", action, service],
        capture_output=True, text=True, check=check,
    )


def _is_service_active(name: str) -> bool:
    service = SYSTEMD_TEMPLATE.format(name=name)
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _instance_exists(name: str) -> bool:
    return (BACKENDS_DIR / name / ".env").exists()


def _read_instance_env(name: str) -> dict:
    env_file = BACKENDS_DIR / name / ".env"
    if not env_file.exists():
        return {}
    result = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            result[k] = v
    return result


def _heartbeat_info(name: str) -> dict | None:
    hb_file = HEARTBEAT_DIR / f"{name}.json"
    if not hb_file.exists():
        return None
    try:
        return json.loads(hb_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


@click.group()
def cli():
    """AgentGate instance management CLI."""
    pass


@cli.command()
@click.argument("name")
@click.option("--channel", default=None, type=click.Choice(["feishu", "telegram"]),
              help="Channel type for routing (optional — omit for HTTP-only backends)")
@click.option("--chat-id", default=None, help="Chat ID for routing (required with --channel)")
@click.option("--workdir", "--work-dir", default=None, type=click.Path(),
              help="Project working directory (default: /tmp/agentgate-<name>)")
@click.option("--port", type=int, default=None, help="Override auto-allocated port")
@click.option("--bot-id", default=None, help="Override auto-detected bot_id")
@click.option("--claude-command", default="claude --dangerously-skip-permissions",
              help="Claude CLI command (default: claude --dangerously-skip-permissions)")
@click.option("--no-start", is_flag=True, help="Only create config, do not start services")
def create(name, channel, chat_id, workdir, port, bot_id, claude_command, no_start):
    """Create a new agent backend instance.

    With --channel and --chat-id: also registers a gateway route.
    Without them: creates a backend-only instance (HTTP API access).
    With --no-start: only creates config files, does not start systemd services.
    """
    config = _load_gateway_config()

    if _instance_exists(name):
        click.echo(f"Error: instance '{name}' already exists (backend dir found)", err=True)
        sys.exit(1)
    if name in config.get("backends", {}):
        click.echo(f"Error: backend '{name}' already exists in gateway config", err=True)
        sys.exit(1)

    # Validate: --chat-id required when --channel is specified
    if channel and not chat_id:
        click.echo("Error: --chat-id is required when --channel is specified", err=True)
        sys.exit(1)

    # 1. Allocate port
    if port is None:
        port = _next_port(config)
    elif port in _allocated_ports(config):
        click.echo(f"Error: port {port} already in use", err=True)
        sys.exit(1)

    # 2. Auto-detect bot_id (only when channel routing is requested)
    if channel and bot_id is None:
        bot_id = _detect_bot_id(config, channel)
        if not bot_id:
            click.echo(f"Error: cannot auto-detect bot_id for channel '{channel}'. "
                        "Use --bot-id to specify.", err=True)
            sys.exit(1)

    # 3. Work directory
    if workdir is None:
        workdir = f"/tmp/agentgate-{name}"
    work_path = Path(workdir)
    work_path.mkdir(parents=True, exist_ok=True)

    # 4. Generate API token
    api_token = f"{name}-{secrets.token_hex(8)}"

    # 5. Create backend .env
    instance_dir = BACKENDS_DIR / name
    instance_dir.mkdir(parents=True, exist_ok=True)
    env_content = (
        f"AGENTGATE_NAME={name}\n"
        f"AGENTGATE_PORT={port}\n"
        f"AGENTGATE_HTTP_PORT={port}\n"
        f"AGENTGATE_API_TOKEN={api_token}\n"
        f"AGENTGATE_CLAUDE_COMMAND={claude_command}\n"
        f"AGENTGATE_PROCESS_NAME=claude\n"
        f"AGENTGATE_TMUX_SESSION_NAME=agentgate-{name}\n"
        f"AGENTGATE_WORK_DIR={workdir}\n"
    )
    (instance_dir / ".env").write_text(env_content)
    click.echo(f"  Created .env at {instance_dir / '.env'}")

    # 6. Update gateway config: add backend
    if "backends" not in config:
        config["backends"] = {}
    # default_window = workdir basename (backend creates tmux window with this name)
    default_window = Path(workdir).name
    config["backends"][name] = {
        "url": f"http://127.0.0.1:{port}",
        "api_token": api_token,
        "agent_type": "claude-code",
        "default_window": default_window,
    }

    # 7. Update gateway config: add route (only if channel specified)
    if channel:
        if "routes" not in config:
            config["routes"] = []
        new_route = {
            "channel": channel,
            "bot_id": bot_id,
            "chat_id": chat_id,
            "backend": name,
        }
        # Check for duplicate route
        for r in config["routes"]:
            if (r.get("channel") == channel and
                r.get("bot_id") == bot_id and
                r.get("chat_id") == chat_id):
                click.echo(f"  Warning: route for ({channel}, {bot_id}, {chat_id}) already exists, "
                            f"updating backend to '{name}'")
                r["backend"] = name
                break
        else:
            config["routes"].append(new_route)

    _save_gateway_config(config)
    click.echo(f"  Updated gateway config: backend '{name}' on port {port}")

    if no_start:
        # Summary for no-start mode
        click.echo(f"\n✓ Instance '{name}' created (not started):")
        click.echo(f"  Port:      {port}")
        click.echo(f"  Workdir:   {workdir}")
        if channel:
            click.echo(f"  Channel:   {channel}")
            click.echo(f"  Chat ID:  {chat_id}")
            click.echo(f"  Bot ID:    {bot_id}")
        click.echo(f"  Service:   {SYSTEMD_TEMPLATE.format(name=name)}")
        return

    # 8. Start backend via systemd
    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"  Starting {service}...")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True)
    _systemctl("enable", service, check=False)
    _systemctl("start", service, check=False)

    # 9. Restart gateway to pick up new config
    click.echo("  Restarting agentgate-gateway...")
    _systemctl("restart", "agentgate-gateway.service", check=False)

    # 10. Summary
    click.echo(f"\n✓ Instance '{name}' created successfully:")
    click.echo(f"  Port:      {port}")
    if channel:
        click.echo(f"  Channel:   {channel}")
        click.echo(f"  Chat ID:  {chat_id}")
        click.echo(f"  Bot ID:    {bot_id}")
    click.echo(f"  Workdir:   {workdir}")
    click.echo(f"  Service:   {service}")


@cli.command("list")
def list_instances():
    """List all agent backend instances."""
    if not BACKENDS_DIR.exists():
        click.echo("No instances found.")
        return

    instances = sorted(d.name for d in BACKENDS_DIR.iterdir()
                       if d.is_dir() and (d / ".env").exists())
    if not instances:
        click.echo("No instances found.")
        return

    config = _load_gateway_config()

    # Header
    click.echo(f"{'NAME':<20} {'PORT':<8} {'STATUS':<12} {'CHANNEL':<12} {'CHAT_ID'}")
    click.echo("-" * 80)

    for name in instances:
        env = _read_instance_env(name)
        port = env.get("AGENTGATE_PORT", "?")
        status = "active" if _is_service_active(name) else "inactive"

        # Find route info
        channel = "-"
        chat_id = "-"
        for r in config.get("routes", []):
            if r.get("backend") == name:
                channel = r.get("channel", "-")
                chat_id = r.get("chat_id", "-")
                break

        click.echo(f"{name:<20} {port:<8} {status:<12} {channel:<12} {chat_id}")


@cli.command()
@click.argument("name")
def status(name):
    """Show detailed status of an instance."""
    if not _instance_exists(name):
        click.echo(f"Error: instance '{name}' not found", err=True)
        sys.exit(1)

    env = _read_instance_env(name)
    service = SYSTEMD_TEMPLATE.format(name=name)
    active = _is_service_active(name)
    hb = _heartbeat_info(name)

    click.echo(f"Instance: {name}")
    click.echo(f"  Port:        {env.get('AGENTGATE_PORT', '?')}")
    click.echo(f"  Service:     {service}")
    click.echo(f"  Status:      {'active' if active else 'inactive'}")
    click.echo(f"  Workdir:     {env.get('AGENTGATE_WORK_DIR', '?')}")
    click.echo(f"  Claude cmd:  {env.get('AGENTGATE_CLAUDE_COMMAND', '?')}")
    if hb:
        click.echo(f"  Heartbeat:   {hb.get('timestamp', '?')}")
    else:
        click.echo(f"  Heartbeat:   no data")

    # Show route info
    config = _load_gateway_config()
    for r in config.get("routes", []):
        if r.get("backend") == name:
            click.echo(f"  Route:       {r['channel']} / {r.get('bot_id', '?')} / {r['chat_id']}")


@cli.command()
@click.argument("name")
def stop(name):
    """Stop an agent backend instance."""
    if not _instance_exists(name):
        click.echo(f"Error: instance '{name}' not found", err=True)
        sys.exit(1)

    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"Stopping {service}...")
    _systemctl("stop", service, check=False)
    click.echo(f"✓ Instance '{name}' stopped.")


@cli.command()
@click.argument("name")
def start(name):
    """Start a stopped agent backend instance."""
    if not _instance_exists(name):
        click.echo(f"Error: instance '{name}' not found", err=True)
        sys.exit(1)

    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"Starting {service}...")
    _systemctl("start", service, check=False)
    click.echo(f"✓ Instance '{name}' started.")


@cli.command()
@click.argument("name")
def restart(name):
    """Restart an agent backend instance."""
    if not _instance_exists(name):
        click.echo(f"Error: instance '{name}' not found", err=True)
        sys.exit(1)

    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"Restarting {service}...")
    _systemctl("restart", service, check=False)
    click.echo(f"✓ Instance '{name}' restarted.")


@cli.command()
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def remove(name, yes):
    """Remove an agent backend instance (stop + cleanup config)."""
    if not _instance_exists(name):
        click.echo(f"Error: instance '{name}' not found", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove instance '{name}'? This will stop the service and "
                       "remove it from gateway config.", abort=True)

    # 1. Stop and disable systemd service
    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"  Stopping {service}...")
    _systemctl("stop", service, check=False)
    _systemctl("disable", service, check=False)

    # 2. Kill tmux session (if exists)
    tmux_session = f"agentgate-{name}"
    result = subprocess.run(
        ["tmux", "kill-session", "-t", tmux_session],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        click.echo(f"  Killed tmux session '{tmux_session}'")

    # 3. Remove from gateway config
    config = _load_gateway_config()
    if name in config.get("backends", {}):
        del config["backends"][name]
    config["routes"] = [r for r in config.get("routes", [])
                        if r.get("backend") != name]
    _save_gateway_config(config)
    click.echo(f"  Removed '{name}' from gateway config")

    # 4. Restart gateway
    click.echo("  Restarting agentgate-gateway...")
    _systemctl("restart", "agentgate-gateway.service", check=False)

    # 5. Clean up heartbeat
    hb_file = HEARTBEAT_DIR / f"{name}.json"
    if hb_file.exists():
        hb_file.unlink()

    # 6. Clean up poll_offsets from gateway DB
    db_path = AGENTGATE_HOME / "gateway" / "messages.db"
    if db_path.exists():
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM poll_offsets WHERE backend_id = ?", (name,))
            conn.commit()
            conn.close()
            click.echo(f"  Cleaned poll_offsets for '{name}'")
        except Exception:
            pass  # Table may not exist yet

    click.echo(f"\n✓ Instance '{name}' removed.")
    click.echo(f"  Note: backend data at {BACKENDS_DIR / name} preserved. "
                "Delete manually if no longer needed.")


if __name__ == "__main__":
    cli()
