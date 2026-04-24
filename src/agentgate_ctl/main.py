"""agentgate-ctl: management CLI for AgentGate backend instances."""

import json
import logging
import secrets
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

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
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )


def _allocated_ports(config: dict) -> set[int]:
    """Collect all ports already used by backends in gateway config."""
    ports = set()
    for bid, bc in config.get("backends", {}).items():
        url = bc.get("url", "")
        # url format: http://127.0.0.1:<port>
        try:
            port = int(url.rsplit(":", 1)[-1])
            ports.add(port)
        except (ValueError, IndexError) as e:
            logger.debug(
                "_allocated_ports: could not parse port from url=%r: %s", url, e
            )
            pass
    # Also check .env files for ports not yet in gateway config
    if BACKENDS_DIR.exists():
        for env_file in BACKENDS_DIR.glob("*/.env"):
            for line in env_file.read_text().splitlines():
                if line.startswith("AGENTGATE_PORT="):
                    try:
                        ports.add(int(line.split("=", 1)[1]))
                    except ValueError as e:
                        logger.debug(
                            "_allocated_ports: could not parse AGENTGATE_PORT in %s: %s",
                            env_file,
                            e,
                        )
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
    elif channel == "dingtalk":
        # DingTalk uses client_id as bot_id
        bots = channels.get("dingtalk", {}).get("bots", [])
        if bots:
            return bots[0].get("client_id", "")
    return ""


def _systemctl(
    action: str, service: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run systemctl command. Default check=True to surface errors.

    Use check=False only when command failure is expected or non-critical
    (e.g., stopping a service that may not exist).
    """
    result = subprocess.run(
        ["sudo", "systemctl", action, service],
        capture_output=True,
        text=True,
        check=check,
    )
    if not result.returncode == 0:
        logger.error(
            "systemctl %s %s failed: %s",
            action,
            service,
            result.stderr.strip(),
        )
    return result


def _is_service_active(name: str) -> bool:
    service = SYSTEMD_TEMPLATE.format(name=name)
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True,
        text=True,
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
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "_heartbeat_info: failed to read heartbeat file %s: %s", hb_file, e
        )
        return None


def _reload_gateway() -> None:
    """Hot-reload gateway config via HTTP API. Warns if gateway is unreachable."""
    config = _load_gateway_config()
    port = config.get("port", 8800)
    api_token = config.get("api_token", "")
    url = f"http://127.0.0.1:{port}/api/admin/reload"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    import urllib.request as _req
    r = _req.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with _req.urlopen(r, timeout=10) as resp:
            data = json.loads(resp.read())
            click.echo(
                f"  Gateway reload: routes={data.get('routes', '?')} "
                f"backends_added={data.get('backends_added', 0)} "
                f"backends_updated={data.get('backends_updated', 0)}"
            )
    except Exception as e:
        click.echo(
            f"  Warning: gateway reload HTTP failed ({e}). "
            "Run: kill -HUP $(pidof agentgate-gateway)",
            err=True,
        )


def _reset_backend_offset(backend_id: str) -> None:
    """Reset gateway in-memory + DB poll offset for a backend via HTTP API."""
    config = _load_gateway_config()
    port = config.get("port", 8800)
    api_token = config.get("api_token", "")
    url = f"http://127.0.0.1:{port}/api/admin/backend/{backend_id}/reset-offset"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    import urllib.request as _req
    r = _req.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with _req.urlopen(r, timeout=10) as resp:
            data = json.loads(resp.read())
            click.echo(
                f"  Reset offset '{backend_id}': old_offset={data.get('old_offset', '?')}"
            )
    except Exception as e:
        click.echo(
            f"  Warning: offset reset for '{backend_id}' failed ({e}). "
            "Gateway may not be running — offset will auto-seed on next startup.",
            err=True,
        )


def _clean_db_offset(backend_id: str) -> None:
    """Delete poll_offsets DB record for a backend (prevents stale offsets on name reuse)."""
    db_path = AGENTGATE_HOME / "gateway" / "messages.db"
    if not db_path.exists():
        return
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM poll_offsets WHERE backend_id = ?", (backend_id,))
        conn.commit()
        conn.close()
        click.echo(f"  Cleaned DB poll_offset for '{backend_id}'")
    except sqlite3.OperationalError as e:
        logger.info("poll_offsets cleanup skipped: %s", e)


@click.group()
def cli():
    """AgentGate instance management CLI."""
    pass


@cli.command()
@click.argument("name")
@click.option(
    "--channel",
    default=None,
    type=click.Choice(["feishu", "telegram", "dingtalk"]),
    help="Channel type for routing (optional — omit for HTTP-only backends)",
)
@click.option(
    "--chat-id", default=None, help="Chat ID for routing (required with --channel)"
)
@click.option(
    "--workdir",
    "--work-dir",
    default=None,
    type=click.Path(),
    help="Project working directory (default: /tmp/agentgate-<name>)",
)
@click.option("--port", type=int, default=None, help="Override auto-allocated port")
@click.option("--bot-id", default=None, help="Override auto-detected bot_id")
@click.option(
    "--agent-type",
    default="claude-code",
    type=click.Choice(["claude-code", "opencode", "qoder"]),
    help="Agent type (default: claude-code)",
)
@click.option(
    "--agent-command",
    default=None,
    help="Agent CLI command (auto-derived from agent-type if not specified)",
)
@click.option(
    "--no-start", is_flag=True, help="Only create config, do not start services"
)
def create(
    name, channel, chat_id, workdir, port, bot_id, agent_type, agent_command, no_start
):
    """Create a new agent backend instance.

    With --channel and --chat-id: also registers a gateway route.
    Without them: creates a backend-only instance (HTTP API access).
    With --no-start: only creates config files, does not start systemd services.

    --agent-type: claude-code, opencode, or qoder (default: claude-code)
    --agent-command: auto-derived from agent-type if not specified
    """
    config = _load_gateway_config()

    if _instance_exists(name):
        click.echo(
            f"Error: instance '{name}' already exists (backend dir found)", err=True
        )
        sys.exit(1)
    if name in config.get("backends", {}):
        click.echo(
            f"Error: backend '{name}' already exists in gateway config", err=True
        )
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
            click.echo(
                f"Error: cannot auto-detect bot_id for channel '{channel}'. "
                "Use --bot-id to specify.",
                err=True,
            )
            sys.exit(1)

    # 3. Work directory
    if workdir is None:
        workdir = f"/tmp/agentgate-{name}"
    work_path = Path(workdir)
    work_path.mkdir(parents=True, exist_ok=True)

    # 4. Generate API token
    api_token = f"{name}-{secrets.token_hex(8)}"

    # 5. Determine agent_command based on agent_type
    if agent_command is None:
        if agent_type == "claude-code":
            agent_command = "claude --dangerously-skip-permissions"
        elif agent_type == "opencode":
            agent_command = "opencode"
        elif agent_type == "qoder":
            agent_command = "qodercli -p --yolo --output-format stream-json"

    # 5. Determine process_name based on agent_type
    if agent_type == "claude-code":
        process_name = "claude"
    elif agent_type == "opencode":
        process_name = "node"  # npm install opencode runs as node
    elif agent_type == "qoder":
        process_name = "qodercli"

    # 6. Create backend .env
    instance_dir = BACKENDS_DIR / name
    instance_dir.mkdir(parents=True, exist_ok=True)
    env_content = (
        f"AGENTGATE_NAME={name}\n"
        f"AGENTGATE_PORT={port}\n"
        f"AGENTGATE_HTTP_PORT={port}\n"
        f"AGENTGATE_API_TOKEN={api_token}\n"
        f"AGENTGATE_AGENT_TYPE={agent_type}\n"
        f"AGENTGATE_AGENT_MODE=tmux\n"
        f"AGENTGATE_PROCESS_NAME={process_name}\n"
        f"AGENTGATE_TMUX_SESSION_NAME=agentgate-{name}\n"
        f"AGENTGATE_WORK_DIR={workdir}\n"
    )
    if agent_type == "claude-code":
        env_content += f"AGENTGATE_CLAUDE_COMMAND={agent_command}\n"
    (instance_dir / ".env").write_text(env_content)
    click.echo(f"  Created .env at {instance_dir / '.env'}")

    # 7. Update gateway config: add backend
    if "backends" not in config:
        config["backends"] = {}
    # default_window = workdir basename (backend creates tmux window with this name)
    default_window = Path(workdir).name
    config["backends"][name] = {
        "url": f"http://127.0.0.1:{port}",
        "api_token": api_token,
        "agent_type": agent_type,
        "default_window": default_window,
    }

    # 8. Update gateway config: add route (only if channel specified)
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
            if (
                r.get("channel") == channel
                and r.get("bot_id") == bot_id
                and r.get("chat_id") == chat_id
            ):
                click.echo(
                    f"  Warning: route for ({channel}, {bot_id}, {chat_id}) already exists, "
                    f"updating backend to '{name}'"
                )
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
        click.echo(f"  Agent type: {agent_type}")
        if channel:
            click.echo(f"  Channel:   {channel}")
            click.echo(f"  Chat ID:  {chat_id}")
            click.echo(f"  Bot ID:    {bot_id}")
        click.echo(f"  Workdir:   {workdir}")
        click.echo(f"  Service:   {SYSTEMD_TEMPLATE.format(name=name)}")
        return

    # 9. Start backend via systemd
    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"  Starting {service}...")
    subprocess.run(
        ["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True
    )
    _systemctl("enable", service)
    _systemctl("start", service)

    # 10. Restart gateway to pick up new config
    click.echo("  Restarting agentgate-gateway...")
    _systemctl("restart", "agentgate-gateway.service", check=False)

    # 11. Summary
    click.echo(f"\n✓ Instance '{name}' created successfully:")
    click.echo(f"  Port:      {port}")
    click.echo(f"  Agent type: {agent_type}")
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

    instances = sorted(
        d.name for d in BACKENDS_DIR.iterdir() if d.is_dir() and (d / ".env").exists()
    )
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
    if env.get("AGENTGATE_AGENT_TYPE"):
        click.echo(f"  Agent type:  {env.get('AGENTGATE_AGENT_TYPE', '?')}")
    if env.get("AGENTGATE_CLAUDE_COMMAND"):
        click.echo(f"  Claude cmd:  {env.get('AGENTGATE_CLAUDE_COMMAND', '?')}")
    if hb:
        click.echo(f"  Heartbeat:   {hb.get('timestamp', '?')}")
    else:
        click.echo(f"  Heartbeat:   no data")

    # Show route info
    config = _load_gateway_config()
    for r in config.get("routes", []):
        if r.get("backend") == name:
            click.echo(
                f"  Route:       {r['channel']} / {r.get('bot_id', '?')} / {r['chat_id']}"
            )


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
        click.confirm(
            f"Remove instance '{name}'? This will stop the service and "
            "remove it from gateway config.",
            abort=True,
        )

    # 1. Stop and disable systemd service
    service = SYSTEMD_TEMPLATE.format(name=name)
    click.echo(f"  Stopping {service}...")
    _systemctl("stop", service)
    _systemctl("disable", service)

    # 2. Kill tmux session (if exists)
    tmux_session = f"agentgate-{name}"
    result = subprocess.run(
        ["tmux", "kill-session", "-t", tmux_session],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        click.echo(f"  Killed tmux session '{tmux_session}'")

    # 3. Remove from gateway config
    config = _load_gateway_config()
    if name in config.get("backends", {}):
        del config["backends"][name]
    config["routes"] = [r for r in config.get("routes", []) if r.get("backend") != name]
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
        except sqlite3.OperationalError as e:
            # Table may not exist yet
            logger.info("poll_offsets table not found (may be first run): %s", e)

    click.echo(f"\n✓ Instance '{name}' removed.")
    click.echo(
        f"  Note: backend data at {BACKENDS_DIR / name} preserved. "
        "Delete manually if no longer needed."
    )


def _gateway_url() -> str:
    """Get the gateway URL from config (for send/health commands)."""
    config = _load_gateway_config()
    # Check if gateway has a port configured
    port = config.get("port", 8800)
    return f"http://127.0.0.1:{port}"


def _gateway_health(gateway_url: str) -> dict | None:
    """Fetch gateway /api/health."""
    import urllib.request

    try:
        req = urllib.request.Request(gateway_url.rstrip("/") + "/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _send_via_gateway(
    gateway_url: str, backend_id: str, text: str, sender: str = ""
) -> dict:
    """Send a message to a backend via gateway HTTP inject API."""
    import urllib.request

    api_url = gateway_url.rstrip("/") + "/api/channel/inject"
    payload = json.dumps(
        {
            "backend_id": backend_id,
            "text": text,
            "sender_name": sender or "agentgate-ctl",
        }
    ).encode()
    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}", "detail": body}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e.reason)}


@cli.command("send")
@click.argument("target", required=False)
@click.argument("message", required=False)
@click.option("--from", "sender", default="", help="Sender name for message envelope")
@click.option("--list", "list_targets", is_flag=True, help="List all available targets")
@click.option(
    "--status", "show_status", is_flag=True, help="Show health status of all backends"
)
def send(target, message, sender, list_targets, show_status):
    """Send a message to an agent backend.

    \b
    Examples:
      agentgate-ctl send my-agent "hello"
      agentgate-ctl send my-agent -            # read from stdin
      agentgate-ctl send --list                # list targets
      agentgate-ctl send --status              # health overview
      echo "long text" | agentgate-ctl send my-agent -
    """
    gateway_url = _gateway_url()

    if list_targets:
        config = _load_gateway_config()
        backends = config.get("backends", {})
        if not backends:
            click.echo("No backends configured.")
            return
        click.echo(f"Gateway: {gateway_url}")
        click.echo(f"{'BACKEND':<25} {'URL':<30} {'TYPE'}")
        click.echo("-" * 70)
        for bid, bc in sorted(backends.items()):
            url = bc.get("url", "?")
            agent_type = bc.get("agent_type", "claude-code")
            click.echo(f"{bid:<25} {url:<30} {agent_type}")
        return

    if show_status:
        health = _gateway_health(gateway_url)
        if not health:
            click.echo(f"Gateway ({gateway_url}) unreachable", err=True)
            sys.exit(1)
        backends = health.get("backends", {})
        if not backends:
            click.echo("No backends.")
            return
        click.echo(f"{'BACKEND':<25} {'STATUS':<12} {'LAST CHECK'}")
        click.echo("-" * 60)
        for bid in sorted(backends):
            info = backends[bid]
            status = info.get("status", "unknown")
            last_check = info.get("last_check", "?")
            if last_check and len(last_check) > 19:
                last_check = last_check[:19]
            marker = "✓" if status == "healthy" else "✗"
            click.echo(f"{bid:<25} {marker} {status:<10} {last_check}")
        return

    if not target:
        click.echo(
            "Error: target backend_id required. Use --list to see available targets.",
            err=True,
        )
        sys.exit(1)

    # Validate target exists
    config = _load_gateway_config()
    if target not in config.get("backends", {}):
        click.echo(f"Error: backend '{target}' not found in gateway config.", err=True)
        click.echo(
            f"Available: {', '.join(sorted(config.get('backends', {})))}", err=True
        )
        sys.exit(1)

    # Read message
    if message is None:
        click.echo("Error: message required. Use '-' to read from stdin.", err=True)
        sys.exit(1)
    if message == "-":
        message = sys.stdin.read().strip()
        if not message:
            click.echo("Error: empty stdin.", err=True)
            sys.exit(1)

    # Send
    result = _send_via_gateway(gateway_url, target, message, sender)

    if result.get("ok"):
        msg_id = result.get("message_id") or result.get("msg_id") or "?"
        click.echo(f"OK → {target} (msg_id={msg_id})")
    else:
        error = result.get("error", "unknown")
        detail = result.get("detail", result.get("msg", ""))
        click.echo(f"FAIL → {target}: {error}", err=True)
        if detail:
            click.echo(f"  {detail[:200]}", err=True)
        sys.exit(1)


@cli.command("switch")
@click.argument("old_backend")
@click.option(
    "--workdir",
    "--work-dir",
    required=True,
    type=click.Path(),
    help="New working directory for the replacement backend",
)
@click.option("--new-name", default=None, help="New backend name (default: <old>-new)")
@click.option(
    "--agent-type",
    default=None,
    type=click.Choice(["claude-code", "opencode", "qoder"]),
    help="Agent type (default: inherited from old backend)",
)
@click.option(
    "--keep-old",
    is_flag=True,
    help="Keep old backend running (only reroute, don't stop/remove)",
)
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def switch(old_backend, workdir, new_name, agent_type, keep_old, dry_run, yes):
    """Switch conversation to a new workspace backend.

    \b
    Creates a new backend with the given workdir, moves all routes from the old
    backend to the new one, resets the gateway poll offset, then stops and removes
    the old backend (unless --keep-old).

    \b
    Examples:
      agentgate-ctl switch proj-dev --workdir ~/proj-v2
      agentgate-ctl switch proj-dev --workdir ~/proj-v2 --new-name proj-dev-v2
      agentgate-ctl switch proj-dev --workdir ~/proj-v2 --keep-old --dry-run
    """
    config = _load_gateway_config()

    # Validate old backend
    if old_backend not in config.get("backends", {}):
        click.echo(f"Error: backend '{old_backend}' not found in gateway config", err=True)
        sys.exit(1)

    old_bc = config["backends"][old_backend]
    old_routes = [r for r in config.get("routes", []) if r.get("backend") == old_backend]

    # Derive new backend name, avoiding collisions
    if new_name is None:
        candidate = f"{old_backend}-new"
        counter = 2
        while candidate in config.get("backends", {}) or _instance_exists(candidate):
            candidate = f"{old_backend}-new{counter}"
            counter += 1
        new_name = candidate
    elif new_name in config.get("backends", {}) or _instance_exists(new_name):
        click.echo(f"Error: backend '{new_name}' already exists", err=True)
        sys.exit(1)

    # Inherit agent_type from old backend config
    if agent_type is None:
        agent_type = old_bc.get("agent_type", "claude-code")

    work_path = Path(workdir).expanduser().resolve()
    default_window = work_path.name
    port = _next_port(config)
    api_token = f"{new_name}-{secrets.token_hex(8)}"

    _PROCESS_NAMES = {"claude-code": "claude", "opencode": "node", "qoder": "qodercli"}
    _COMMANDS = {
        "claude-code": "claude --dangerously-skip-permissions",
        "opencode": "opencode",
        "qoder": "qodercli -p --yolo --output-format stream-json",
    }
    process_name = _PROCESS_NAMES.get(agent_type, "claude")
    agent_command = _COMMANDS.get(agent_type, "claude --dangerously-skip-permissions")

    # Dry run: print plan only
    if dry_run:
        click.echo("--- DRY RUN (no changes made) ---")
        click.echo(f"CREATE  backend:       {new_name}")
        click.echo(f"        workdir:       {work_path}")
        click.echo(f"        port:          {port}")
        click.echo(f"        agent_type:    {agent_type}")
        click.echo(f"        default_window:{default_window}")
        if old_routes:
            click.echo(f"REROUTE {len(old_routes)} route(s): {old_backend} → {new_name}")
            for r in old_routes:
                click.echo(f"        {r['channel']}/{r.get('bot_id', '?')}/{r['chat_id']}")
        else:
            click.echo(f"  (no routes to reroute from '{old_backend}')")
        click.echo(f"RELOAD  gateway + reset offset for '{new_name}'")
        if not keep_old:
            click.echo(f"REMOVE  backend '{old_backend}': stop service, clean offset")
        return

    # Confirmation (skip if --yes or no routes being moved)
    if not yes and old_routes:
        route_summary = ", ".join(
            f"{r['channel']}/{r['chat_id']}" for r in old_routes
        )
        click.confirm(
            f"Switch {len(old_routes)} route(s) ({route_summary}) "
            f"from '{old_backend}' to new backend '{new_name}'?",
            abort=True,
        )

    # 1. Create workdir
    work_path.mkdir(parents=True, exist_ok=True)

    # 2. Write .env for new backend
    instance_dir = BACKENDS_DIR / new_name
    instance_dir.mkdir(parents=True, exist_ok=True)
    env_content = (
        f"AGENTGATE_NAME={new_name}\n"
        f"AGENTGATE_PORT={port}\n"
        f"AGENTGATE_HTTP_PORT={port}\n"
        f"AGENTGATE_API_TOKEN={api_token}\n"
        f"AGENTGATE_AGENT_TYPE={agent_type}\n"
        f"AGENTGATE_AGENT_MODE=tmux\n"
        f"AGENTGATE_PROCESS_NAME={process_name}\n"
        f"AGENTGATE_TMUX_SESSION_NAME=agentgate-{new_name}\n"
        f"AGENTGATE_WORK_DIR={work_path}\n"
    )
    if agent_type == "claude-code":
        env_content += f"AGENTGATE_CLAUDE_COMMAND={agent_command}\n"
    (instance_dir / ".env").write_text(env_content)
    click.echo(f"  Created .env: {instance_dir / '.env'}")

    # 3. Update gateway config: add new backend + reroute
    config.setdefault("backends", {})[new_name] = {
        "url": f"http://127.0.0.1:{port}",
        "api_token": api_token,
        "agent_type": agent_type,
        "default_window": default_window,
    }
    for r in config.get("routes", []):
        if r.get("backend") == old_backend:
            r["backend"] = new_name
    _save_gateway_config(config)
    click.echo(f"  Gateway config: added '{new_name}', rerouted {len(old_routes)} route(s)")

    # 4. Create tmux session (backend creates the work window, but not the session itself)
    tmux_session = f"agentgate-{new_name}"
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_session, "-n", "__main__"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "duplicate session" not in result.stderr:
        click.echo(
            f"  Warning: tmux session creation failed: {result.stderr.strip()}", err=True
        )
    else:
        click.echo(f"  Created tmux session: {tmux_session}")

    # 5. Start new backend service
    new_service = SYSTEMD_TEMPLATE.format(name=new_name)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True, check=True)
    _systemctl("enable", new_service)
    _systemctl("start", new_service)
    click.echo(f"  Started {new_service}")

    # 6. Hot-reload gateway (picks up new backend + updated routes)
    _reload_gateway()

    # 7. Reset offset for new backend (defensive: auto-seeded anyway, but explicit is safer)
    _reset_backend_offset(new_name)

    # 8. Remove old backend (unless --keep-old)
    if not keep_old:
        # Clean DB first (prevents stale offset if name is ever reused)
        _clean_db_offset(old_backend)
        # Reset in-memory offset so poller stops replaying if it reconnects before stopping
        _reset_backend_offset(old_backend)
        # Stop and disable service
        old_service = SYSTEMD_TEMPLATE.format(name=old_backend)
        _systemctl("stop", old_service, check=False)
        _systemctl("disable", old_service, check=False)
        click.echo(f"  Stopped {old_service}")
        # Remove from gateway config + reload (removes from routing table)
        config = _load_gateway_config()
        config.get("backends", {}).pop(old_backend, None)
        _save_gateway_config(config)
        _reload_gateway()
        click.echo(f"  Removed '{old_backend}' from gateway config")

    click.echo(f"\n✓ Switch complete:")
    click.echo(f"  Old backend: {old_backend} ({'kept' if keep_old else 'stopped + removed'})")
    click.echo(f"  New backend: {new_name} (port {port})")
    click.echo(f"  Workdir:     {work_path}")
    click.echo(f"  Routes:      {len(old_routes)} moved")
    click.echo(f"\nVerify:")
    click.echo(
        f"  curl http://127.0.0.1:{port}/api/health "
        f"-H 'Authorization: Bearer {api_token}'"
    )


@cli.command("reroute")
@click.argument("from_backend")
@click.argument("to_backend")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing")
def reroute(from_backend, to_backend, dry_run):
    """Move all routes from one backend to another (both must already exist).

    \b
    Useful when you have two configured backends and want to switch which one
    handles a conversation, without creating new instances.
    Resets the gateway poll offset for the target backend after rerouting.

    \b
    Example:
      agentgate-ctl reroute proj-dev proj-dev-v2
    """
    config = _load_gateway_config()
    backends = config.get("backends", {})

    if from_backend not in backends:
        click.echo(f"Error: backend '{from_backend}' not found", err=True)
        sys.exit(1)
    if to_backend not in backends:
        click.echo(f"Error: backend '{to_backend}' not found", err=True)
        sys.exit(1)

    affected = [r for r in config.get("routes", []) if r.get("backend") == from_backend]
    if not affected:
        click.echo(f"Warning: '{from_backend}' has no routes — nothing to reroute", err=True)

    if dry_run:
        click.echo("--- DRY RUN (no changes made) ---")
        click.echo(f"REROUTE {len(affected)} route(s): {from_backend} → {to_backend}")
        for r in affected:
            click.echo(f"  {r['channel']}/{r.get('bot_id', '?')}/{r['chat_id']}")
        click.echo(f"RELOAD  gateway + reset offset for '{to_backend}'")
        return

    for r in config.get("routes", []):
        if r.get("backend") == from_backend:
            r["backend"] = to_backend

    _save_gateway_config(config)
    click.echo(f"  Updated {len(affected)} route(s): {from_backend} → {to_backend}")

    _reload_gateway()
    _reset_backend_offset(to_backend)

    click.echo(f"\n✓ Rerouted {len(affected)} route(s): {from_backend} → {to_backend}")
    for r in affected:
        click.echo(f"  {r['channel']}/{r.get('bot_id', '?')}/{r['chat_id']}")


if __name__ == "__main__":
    cli()
