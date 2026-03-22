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

```bash
pip install im-agent-gate
```

```yaml
# config.yaml
backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: my-secret-token
    default_window: main

channels:
  telegram:
    bot_token: "123456:ABC-DEF..."

routes:
  - channel: telegram
    bot_id: "123456"
    chat_id: "-100123456789"
    backend: my-agent
```

```bash
# Start a backend (manages the agent process)
agentgate-backend --name my-agent --port 8903 --work-dir ~/my-project

# Start the gateway (connects IM channels to backends)
agentgate-gateway --config config.yaml
```

Send a message in your Telegram group. The agent gets it, works on it, and the reply shows up in the same group.

### HTTP-only mode (no IM needed)

Don't use IM? Skip the channel config entirely. Control agents via HTTP:

```bash
# Send a message to an agent
curl -X POST http://localhost:8800/api/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "text": "refactor the auth module"}'

# Read agent output
curl "http://localhost:8903/api/output/main?since=0"
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

Claude Code for complex work, OpenCode with qwen-plus for routine tasks at roughly 1/20 the cost:

```yaml
backends:
  project-dev:
    agent_type: claude-code    # complex refactoring, architecture
    agent_mode: tmux

  project-qa:
    agent_type: opencode       # log analysis, test runs, boilerplate
    agent_mode: subprocess
    model: qwen-plus
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
│  │  CC + tmux    │  │  OC + sub    │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**Gateway** — one instance. Handles channels, routing, output polling, persistence, recovery.

**Backend** — one per agent. Manages the agent process, health checks, and exposes inject/output over HTTP.

The two layers talk over HTTP. Same machine or different machines, your call.

---

## API

### Gateway

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/health` | System status — channels, backends, pending messages |
| POST | `/api/messages/query` | Query message history |
| POST | `/api/admin/reload` | Reload config without restart |
| POST | `/api/inject` | Send a message to a backend (bypasses IM) |

### Backend

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/health` | Backend and agent health |
| POST | `/api/inject` | Send message to the agent |
| GET | `/api/output/{window}?since={offset}` | Read new output since offset |

---

## Roadmap

- More agents — Aider, Cursor CLI, custom agents via the driver protocol
- More channels — Discord, Slack, WeChat Work
- Web dashboard — route management, health overview, message browser
- Distributed deployment — backends are standalone HTTP services, already deployable across hosts

---

## Acknowledgments

AgentGate grew out of [ccbot](https://github.com/six-ddc/ccbot), a Telegram-to-tmux bridge for Claude Code. The backend's process management, crash recovery, and health monitoring were built and tested in ccbot before being generalized here.
