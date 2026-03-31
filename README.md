# AgentGate

**Turn IM and HTTP into control channels for your CLI AI agents.**

Send a message in Feishu or Telegram, your agent gets it. Call an HTTP endpoint, your agent gets it. Agent produces output, it shows up in your chat or API response. No terminal required.

[中文文档](README.zh-CN.md)

---

## What it does

AgentGate sits between your IM / HTTP clients and your CLI agents (Claude Code, OpenCode, etc.):

```
Feishu / Telegram / HTTP  →  AgentGate Gateway  →  Agent Backend  →  CLI Agent
                          ←  (output pushed back)  ←                ←
```

**For end users:** message a bot in your IM group, the agent works on it, replies appear in the same group. As natural as chatting with a colleague.

**For developers:** `POST /api/inject` sends a message to an agent, `GET /api/output/{window}` reads its output. Build any automation on top — CI hooks, monitoring scripts, custom UIs.

**For teams:** one gateway routes messages from multiple IM groups to multiple agent instances. Two projects, four agents, one IM app — each group talks to its own agent, fully isolated.

---

## Quick start

### Install

```bash
pip install im-agent-gate
```

### 1. Create a backend instance

Each agent needs a backend instance. Create the instance directory and `.env` config:

```bash
mkdir -p ~/.agentgate/backends/my-agent
cat > ~/.agentgate/backends/my-agent/.env << 'EOF'
AGENTGATE_NAME=my-agent
AGENTGATE_PORT=8903
AGENTGATE_HTTP_PORT=8903
AGENTGATE_API_TOKEN=my-secret-token
AGENTGATE_WORK_DIR=/path/to/your/project
AGENTGATE_TMUX_SESSION_NAME=agentgate-my-agent
AGENTGATE_AGENT_TYPE=claude-code
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_CLAUDE_COMMAND=claude --dangerously-skip-permissions
AGENTGATE_PROCESS_NAME=claude
EOF
```

Key settings:

| Setting | Description |
|---------|-------------|
| `AGENTGATE_NAME` | Unique instance name |
| `AGENTGATE_PORT` / `HTTP_PORT` | HTTP API port (must match, one per instance) |
| `AGENTGATE_API_TOKEN` | Bearer token for gateway ↔ backend auth |
| `AGENTGATE_WORK_DIR` | Agent's working directory |
| `AGENTGATE_AGENT_TYPE` | `claude-code` or `opencode` |
| `AGENTGATE_AGENT_MODE` | `tmux` (persistent session) or `subprocess` (stdin/stdout) |
| `AGENTGATE_PROCESS_NAME` | Process name for health detection (see below) |

**`AGENTGATE_PROCESS_NAME`** controls how SelfMonitor detects whether the agent is alive. It must match what `tmux` reports as the foreground process in the pane (`pane_current_command`). If it doesn't match, SelfMonitor will think the agent is dead and keep restarting it.

Defaults by agent type (auto-derived, usually no need to set):

| Agent type | Default | Why |
|------------|---------|-----|
| `claude-code` | `claude` | Claude Code binary |
| `opencode` | `node` | npm-installed OpenCode runs as a Node.js process |

Override when needed — e.g. native-compiled OpenCode binary shows `opencode` in tmux, not `node`:

```bash
AGENTGATE_PROCESS_NAME=opencode
```

To check what tmux actually reports: `tmux list-windows -F '#{window_name} #{pane_current_command}'`

For OpenCode with a local model:

```bash
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3-32B
# AGENTGATE_PROCESS_NAME=node  (default for opencode, override to "opencode" for native binary)
```

OpenCode instances get automatic permission configuration — all tools allowed, interactive prompts disabled (they can't be answered via IM).

### 2. Configure the gateway

```yaml
# ~/.agentgate/gateway/config.yaml

backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: my-secret-token
    default_window: my-project    # must match WORK_DIR basename

channels:
  telegram:
    bots:
      - bot_id: my_bot
        bot_token: "123456:ABC-DEF..."
        proxy: "http://127.0.0.1:7897"   # optional, for regions that need it

routes:
  - channel: telegram
    bot_id: my_bot
    chat_id: "7003732745"       # user or group chat ID
    backend: my-agent
```

**Important:** `default_window` must equal the basename of `WORK_DIR`. If `WORK_DIR=/home/user/my-project`, then `default_window: my-project`. Mismatch = output never reaches IM.

### 3. Start

```bash
# Create tmux session (tmux mode only, first time)
tmux new-session -d -s agentgate-my-agent -n __main__

# Start the backend
agentgate-backend --name my-agent

# Start the gateway
agentgate-gateway --config ~/.agentgate/gateway/config.yaml
```

Or use systemd (recommended for production):

```bash
# Template service included in deploy/
sudo cp deploy/agentgate-backend@.service /etc/systemd/system/
sudo cp deploy/agentgate-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now agentgate-backend@my-agent
sudo systemctl enable --now agentgate-gateway
```

### 4. Verify

```bash
# Check backend health
curl http://127.0.0.1:8903/api/health -H "Authorization: Bearer my-secret-token"

# Check gateway health
curl http://127.0.0.1:8800/api/health
```

Send a message in your Telegram chat. The agent gets it, works on it, and the reply shows up in the same chat.

### HTTP-only mode (no IM needed)

Skip the channel config entirely. Control agents via HTTP:

```bash
# Send a message to an agent
curl -X POST http://localhost:8800/api/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "text": "refactor the auth module"}'

# Read agent output
curl "http://localhost:8903/api/output/my-project?since=0" \
  -H "Authorization: Bearer my-secret-token"
```

---

## Routing

Messages are routed by a `(channel, bot, chat)` triplet — meaning the **same bot** in **different groups** can talk to **different agents**. This is the key to scaling without bot sprawl:

- **No bot-per-agent overhead.** You don't need to create a new bot for each agent. One bot covers all your projects — just put it in different groups.
- **Group = context.** Users talk in whichever group matches their project. The routing is invisible — they just send a message and the right agent responds.
- **Add projects in seconds.** New project? Create a group, add the bot, add one route config line, hot-reload. No bot registration, no token management.

```yaml
routes:
  - channel: feishu
    bot_id: cli_Xxxxx
    chat_id: oc_fish_dev_group
    backend: fish-dev            # → fish project dev agent

  - channel: feishu
    bot_id: cli_Xxxxx            # same bot
    chat_id: oc_trade_dev_group  # different group
    backend: trade-dev           # → trade project dev agent
```

Update the config and hot-reload without restarting:

```bash
kill -HUP $(pidof agentgate-gateway)
# or
curl -X POST http://localhost:8800/api/admin/reload
```

IM connections stay alive. No messages dropped.

---

## Agent-agnostic

AgentGate doesn't care what CLI agent you run. The `AgentDriver` protocol abstracts agent differences:

| Agent | Mode | How output is read |
|-------|------|--------------------|
| Claude Code | tmux | JSONL file polling |
| Claude Code | subprocess | stream-json stdout |
| OpenCode | tmux | SQLite WAL query |
| OpenCode | subprocess | stream-json stdout |

Adding a new agent type means implementing the `AgentDriver` protocol — about 200 lines. No framework changes.

### Mix agents, cut costs

Claude Code for complex work, OpenCode with a local model (e.g. Qwen3.5 via llama-server) for routine tasks at a fraction of the cost:

```bash
# Backend 1: Claude Code for architecture work
AGENTGATE_AGENT_TYPE=claude-code
AGENTGATE_AGENT_MODE=tmux

# Backend 2: OpenCode + local Qwen3.5 for routine tasks
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3.5-35B
```

Same IM interface, same routing, same message persistence. The person sending messages doesn't need to know which agent is on the other end.

---

## Two modes: tmux and subprocess

| | tmux | subprocess |
|---|---|---|
| Mid-task correction | Yes, between tool calls | Queued until turn ends |
| Live terminal view | `tmux attach` | No |
| Requires tmux | Yes | No |
| Output latency | ~2s (file polling) | Real-time (stdout streaming) |

**tmux mode** runs the agent in a persistent tmux session. You can `tmux attach` to watch it work, and send corrections mid-task — the agent picks them up between tool calls. Best for high-stakes work where you want full visibility.

**subprocess mode** runs the agent as a stdin/stdout child process. Simpler setup, no tmux dependency, real-time streaming output. Best for routine tasks or headless automation.

Pick per backend. Both modes get the same routing, persistence, and API.

---

## Message persistence

Every message is written to SQLite before it's processed — inbound and outbound. Status goes `pending` → `delivered` or `failed`.

```bash
curl -X POST http://localhost:8800/api/messages/query \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "page_size": 20}'
```

Full audit trail for debugging and compliance.

---

## High availability

Five independent layers — each one works regardless of the others:

| Layer | What it covers | How |
|-------|---------------|-----|
| L0 | Process survival | systemd `Restart=always` |
| L1 | IM connections | Per-adapter auto-reconnect with exponential backoff |
| L2 | Backend health | Continuous probing; auto-pause/resume on failure/recovery |
| L3 | Message delivery | Persist-before-send, retry with confirmation, alert on repeated failure |
| L4 | Observability | `/health` endpoint with full system status |

Feishu going down doesn't affect Telegram. A backend crash doesn't take down the gateway. Agent auth expires overnight? AgentGate retries quietly for hours. New messages during outage get `503` — nothing silently disappears.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Channel Gateway                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │  Feishu   │  │ Telegram │  │   HTTP   │  │  Adapter…  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘  │
│       │              │             │              │         │
│  ┌────▼──────────────▼─────────────▼──────────────▼──────┐  │
│  │              Router  (channel, bot, chat) → backend    │  │
│  └────┬──────────────────────────────────────────────┬───┘  │
│       │ inject                            poll output│      │
│  ┌────▼────┐    ┌──────────┐    ┌──────────┐   ┌────▼────┐  │
│  │ Inbound │    │ Recovery │    │  SQLite  │   │ Output  │  │
│  │ Handler │    │ Manager  │    │          │   │ Poller  │  │
│  └────┬────┘    └──────────┘    └──────────┘   └────┬────┘  │
└───────┼─────────────────────────────────────────────┼───────┘
        │  HTTP API                        HTTP API   │
┌───────▼─────────────────────────────────────────────▼───────┐
│                    Agent Backends (per instance)             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  project-dev  │  │  project-qa  │  │  trade-dev   │       │
│  │  CC + tmux    │  │  OC + tmux   │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**Gateway** — one instance. Handles channels, routing, output polling, persistence, recovery.

**Backend** — one per agent. Manages the agent process, health checks, and exposes inject/output over HTTP.

The two layers talk over HTTP. Same machine or different machines, your call.

---

## CLI management (agentgate-ctl)

### Instance lifecycle

```bash
# Create a new backend instance (auto-allocates port, generates token)
agentgate-ctl create my-agent --work-dir ~/my-project --channel telegram --chat-id "7003732745"

# List all instances
agentgate-ctl list

# Instance status / start / stop / restart / remove
agentgate-ctl status my-agent
agentgate-ctl stop my-agent
agentgate-ctl start my-agent
agentgate-ctl restart my-agent
agentgate-ctl remove my-agent
```

### Messaging between agents

```bash
# Send a message to any backend by ID
agentgate-ctl send my-agent "refactor the auth module"

# Read message from stdin (for long content)
echo "detailed instructions..." | agentgate-ctl send my-agent -

# List all available backends
agentgate-ctl send --list

# Health overview of all backends
agentgate-ctl send --status

# With sender name (for message envelope)
agentgate-ctl send --from dev my-qa-agent "please verify the fix"
```

---

## OpenCode + local model setup

Run OpenCode with a local model (e.g. Qwen3.5 via llama-server) as an agentgate backend:

### 1. Start llama-server

```bash
llama-server -m Qwen3.5-35B-A3B.gguf --port 18090 --ctx-size 262144 --parallel 2
```

### 2. Configure OpenCode provider

Add the local provider to `~/.config/opencode/opencode.json`:

```json
{
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local llama-server",
      "options": {
        "baseURL": "http://127.0.0.1:18090/v1",
        "apiKey": "dummy"
      },
      "models": {
        "Qwen3.5-35B": {
          "name": "Qwen3.5-35B",
          "attachments": false,
          "limit": {
            "context": 131072,
            "output": 8192
          }
        }
      }
    }
  }
}
```

Set `limit.context` to your llama-server's `ctx-size / parallel` (e.g. 262144 / 2 = 131072).

### 3. Create the backend

```bash
# .env
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3.5-35B
AGENTGATE_PROCESS_NAME=node
```

Or via CLI:

```bash
agentgate-ctl create local-llm --work-dir ~/my-workspace
# Then edit ~/.agentgate/backends/local-llm/.env to set agent_type/mode/model
```

AgentGate automatically configures OpenCode permissions for managed instances — all tools allowed, interactive prompts (AskUser) disabled since they can't be answered via IM.

### 4. Project-level model override

Each backend's working directory can have its own `opencode.json` to override the model:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local/Qwen3.5-35B"
}
```

This lets different backends use different models while sharing the global provider config.

---

## API

### Gateway

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/health` | System status — channels, backends, pending messages |
| POST | `/api/messages/query` | Query message history with filters |
| POST | `/api/admin/reload` | Reload config without restart |
| POST | `/api/channel/inject` | Send a message to a backend (bypasses IM) |

### Backend

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/api/health` | Backend and agent health |
| POST | `/api/inject` | Send message to the agent |
| GET | `/api/output/{window}?since={offset}` | Read new output since offset |

---

## Troubleshooting

### Agent replies not reaching IM

Check the output pipeline logs. Every log line includes a trace ID (`[traceID]`) for cross-service correlation:

```bash
# By backend ID — see all activity for one agent
journalctl -u agentgate-gateway --since "10 min ago" | grep "backend=my-agent"

# By trace ID — see the full cross-service journey of one poll cycle
journalctl -u agentgate-gateway -u agentgate-backend@my-agent | grep "abc123def456"

# By poll ID — see everything in one poll cycle
journalctl -u agentgate-gateway | grep "poll=e521e8b3"
```

A healthy message shows 4 log lines:

1. `[traceID] Polled N new messages from backend=my-agent poll=xxx` — poller read output
2. `[traceID] Outbound save: msg_id=xxx backend=my-agent poll=xxx → channel:bot chat=xxx` — persisted
3. `[traceID] TG outbound [bot]: chat_id=xxx text=xxx` — push started
4. `TG send ok [bot]: elapsed=xxxms` — push succeeded

Missing step tells you where it broke:

| Missing | Likely cause |
|---------|-------------|
| No `Polled` | Wrong `default_window` (must match WORK_DIR basename) or session_id mismatch |
| `Polled` but no `Outbound save` | Content deduped (`dedup skip` in logs) or all messages filtered (`filtered to 0 text`) |
| `Outbound save` but no `TG outbound` | Push task error (`Push task failed` in logs) |
| `TG outbound` but no `send ok` | Telegram API error (timeout, rate limit, proxy issue) |

---

## Roadmap

- More agents — Aider, Cursor CLI, custom agents via the driver protocol
- More channels — Discord, Slack, WeChat Work
- Web dashboard — route management, health overview, message browser
- Distributed deployment — backends are standalone HTTP services, already deployable across hosts

---

## Acknowledgments

AgentGate grew out of [ccbot](https://github.com/six-ddc/ccbot), a Telegram-to-tmux bridge for Claude Code. The backend's process management, crash recovery, and health monitoring were built and tested in ccbot before being generalized here.
