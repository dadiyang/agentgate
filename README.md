# AgentGate

**Production-grade multi-channel gateway for CLI AI agents.**

Talk to your CLI agents from Feishu or Telegram. See what they're doing, correct them mid-task, let them crash and recover on their own.

[中文文档](README.zh-CN.md)

---

## The problem

You run Claude Code in a tmux session (a terminal multiplexer that keeps processes alive after you disconnect — if you haven't used it, think of it as a persistent terminal in the background). The agent keeps working even when you close your laptop. Good.

But then you're at lunch and want to check how the refactoring is going. You pull out your phone, open an SSH app, squint at a tiny terminal, type `tmux attach`... it's awful. Sending a nuanced correction through a phone SSH keyboard is even worse.

The agent is running fine. You just can't easily talk to it.

A few more things that get old fast:

- **Agent breaks, process doesn't.** OAuth token expires, context window fills up, API rate limit kicks in. The tmux session is still there, but the agent is stuck at a prompt. You find out hours later.
- **Four agents, four sessions.** Two projects, two agents each. SSH in, attach, check, detach, attach another one. Just to see who said what.
- **Scroll buffer is gone.** What did the agent decide yesterday? Why did it take that approach? The terminal doesn't remember.

AgentGate puts your IM in front of all this. Send a message in Feishu or Telegram, the agent gets it. Agent produces output, it shows up in your chat. Agent crashes at 3am, AgentGate restarts it and you get a notification.

---

## Mid-task correction

This is the main reason AgentGate uses tmux.

AI agents make their own decisions — which files to edit, what approach to take. Sometimes they go wrong. In a normal setup, you wait for it to finish (or kill it), then start over. With AgentGate's tmux mode, you send a message while the agent is working, and it picks up your correction between tool calls:

```
You:   Refactor the auth module to use JWT
Agent: [step 1 done... step 2 done... step 3 running...]
You:   Wait — keep the session token fallback for legacy clients
Agent: Got it, adjusting...
       [continues with the corrected plan]
```

This works because tmux lets AgentGate write into the agent's terminal buffer. When the agent finishes a tool call and checks for new input, it sees your message. It's the same mechanism as if you'd typed it at the keyboard yourself.

Subprocess mode (stdin/stdout, no tmux) can't do this — the stream-json protocol is strictly request-response. Messages sent mid-turn get queued until the current turn ends. This isn't an AgentGate limitation; it's how the protocol works. The most popular tool in this space has the same constraint, with a comment in their source code: "do NOT send to agent stdin yet."

### Live observation

`tmux attach` shows you exactly what the agent sees — files being read, tools being called, decisions being made. For a 30-minute task, you can glance at it anytime to check direction, instead of waiting until the end.

### Direct intervention

The tmux session is a regular terminal. Open a new pane, run `git diff`, manually fix something, run a diagnostic. You and the agent share the same workspace. And everything that happens in tmux gets pushed to IM too — your team sees the full picture.

---

## Routing

Messages are routed by a `(channel, bot, chat)` triplet. One bot handles multiple projects — messages in the `fish` group go to `fish-dev`, messages in the `trade` group go to `trade-dev`. Each agent instance is isolated.

```yaml
routes:
  - channel: feishu
    bot_id: cli_Xxxxx
    chat_id: oc_fish_dev_group
    backend_id: fish-dev

  - channel: feishu
    bot_id: cli_Xxxxx          # same bot
    chat_id: oc_trade_dev_group  # different group
    backend_id: trade-dev        # different agent
```

Two projects, four agents, one IM app. Update the config and reload without restarting:

```bash
kill -HUP $(pidof agentgate-gateway)
# or
curl -X POST http://localhost:8800/api/admin/reload
```

IM connections stay alive. No messages dropped.

---

## Mix agents, cut costs

If you're using both Claude Code and OpenCode (or similar tools backed by cheaper models like qwen-plus), you probably have different setups for each. AgentGate doesn't care what agent is behind a backend — the `AgentDriver` protocol abstracts it away.

A common setup: Claude Code for complex work, OpenCode with qwen-plus for routine tasks at roughly 1/20 the cost.

```yaml
backends:
  fish-dev:
    agent_type: claude-code    # complex refactoring, architecture
    agent_mode: tmux

  fish-qa:
    agent_type: opencode       # log analysis, test runs, boilerplate
    agent_mode: subprocess
    model: qwen-plus
```

Same IM interface, same routing, same message persistence. The user sending messages doesn't need to know or care which agent is on the other end.

Built-in drivers:

| Agent | Mode | How output is read |
|-------|------|--------------------|
| Claude Code | tmux | JSONL file polling |
| Claude Code | subprocess | stream-json stdout |
| OpenCode | tmux | SQLite WAL query |
| OpenCode | subprocess | stream-json stdout |

Adding a new agent type means implementing the `AgentDriver` protocol — about 200 lines. No framework changes.

---

## tmux vs subprocess

Not every task needs real-time oversight. AgentGate supports both modes:

| | tmux | subprocess |
|---|---|---|
| Mid-task correction | Yes, between tool calls | Queued until turn ends |
| Live terminal view | `tmux attach` | No |
| Requires tmux installed | Yes | No |
| Output latency | ~2s (file polling) | Real-time (stdout streaming) |

Pick per backend. High-stakes refactoring? tmux. Running tests or generating boilerplate? subprocess is simpler.

---

## Message persistence

Every message is written to SQLite before it's processed — inbound and outbound. Status goes `pending` → `delivered` or `failed`.

```bash
curl -X POST http://localhost:8800/api/messages/query \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "fish-dev", "page_size": 20}'
```

What did the agent receive? What did it output? When did delivery fail? It's all there.

---

## High availability

Five independent layers:

| Layer | What it covers | How |
|-------|---------------|-----|
| L0 | Process survival | systemd `Restart=always` |
| L1 | IM connections | Each adapter reconnects independently, exponential backoff |
| L2 | Backend health | Probes backends continuously; pauses polling on failure, resumes on recovery |
| L3 | Message delivery | Persists before sending, retries with confirmation, alerts on repeated failure |
| L4 | Observability | `/health` endpoint with full system status |

Feishu going down doesn't affect Telegram. A backend crash doesn't take down the gateway. If an agent's auth token expires overnight, AgentGate retries quietly for hours. New messages during the outage get a `503` — nothing silently disappears.

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
│  │  fish-dev     │  │  fish-qa     │  │  trade-dev   │       │
│  │  CC + tmux    │  │  OC + sub    │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**Gateway** — one instance. Handles channels, routing, output polling, persistence, crash recovery.

**Backend** — one per agent. Manages the agent process, health checks, and exposes inject/output over HTTP.

The two layers talk over HTTP. Same machine or different machines, your call.

---

## Quick start

### What you need

- Python 3.11+
- tmux (if using tmux mode)
- A CLI agent (Claude Code, OpenCode, etc.)

### Install

```bash
git clone https://github.com/anthropics/agentgate.git
cd agentgate
pip install .
```

### Configure

```yaml
# config.yaml
backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: my-secret-token
    default_window: main

channels:
  telegram:
    type: telegram
    bot_token: "123456:ABC-DEF..."

routes:
  - channel: telegram
    bot_id: "123456"
    chat_id: "-100123456789"
    backend_id: my-agent
```

### Run

```bash
# Start a backend
agentgate-backend --instance-dir ~/.agentgate/backends/my-agent \
  --agent-type claude-code --agent-mode tmux \
  --work-dir ~/my-project

# Start the gateway
agentgate-gateway --config config.yaml
```

Send a message in your Telegram group. The agent gets it, works on it, and the reply shows up in the same group.

---

## API

### Gateway

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/health` | System status — channels, backends, pending messages |
| POST | `/api/messages/query` | Query message history |
| POST | `/api/admin/reload` | Reload config without restart |
| POST | `/api/inject` | Send a message to a backend directly (bypasses IM) |

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
