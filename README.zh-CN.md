# AgentGate

**把 IM 和 HTTP 变成 CLI AI Agent 的控制通道。**

在飞书或 Telegram 里发一条消息，Agent 就收到了。调一个 HTTP 接口，Agent 也收到了。Agent 有输出，直接推到你的聊天窗口或 API 响应里。不需要开终端。

[English](README.md)

---

## 它做什么

AgentGate 架在你的 IM / HTTP 客户端和 CLI Agent（Claude Code、OpenCode 等）之间：

```
飞书 / Telegram / HTTP  →  AgentGate 网关  →  Agent 后端  →  CLI Agent
                        ←  （输出推回）      ←              ←
```

**对终端用户：** 在 IM 群里 @ 机器人发消息，Agent 处理完回复出现在同一个群里。跟找同事聊天一样自然。

**对开发者：** `POST /api/inject` 给 Agent 发消息，`GET /api/output/{window}` 读输出。CI 钩子、监控脚本、自定义 UI——随便往上搭。

**对团队：** 一个网关把多个 IM 群的消息路由到多个 Agent 实例。两个项目四个 Agent，一个 IM 入口——每个群对接自己的 Agent，完全隔离。

---

## 快速开始

### 安装

```bash
pip install im-agent-gate
```

### 1. 创建后端实例

每个 Agent 需要一个后端实例。创建实例目录和 `.env` 配置：

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

主要配置项：

| 配置项 | 说明 |
|--------|------|
| `AGENTGATE_NAME` | 实例唯一标识 |
| `AGENTGATE_PORT` / `HTTP_PORT` | HTTP API 端口（两个必须一致，每个实例独占） |
| `AGENTGATE_API_TOKEN` | gateway ↔ backend 的认证 token |
| `AGENTGATE_WORK_DIR` | Agent 的工作目录 |
| `AGENTGATE_AGENT_TYPE` | `claude-code` 或 `opencode` |
| `AGENTGATE_AGENT_MODE` | `tmux`（持久会话）或 `subprocess`（stdin/stdout） |

OpenCode + 本地模型的配置：

```bash
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3-32B
AGENTGATE_PROCESS_NAME=node
```

OpenCode 实例会自动配置权限——所有工具允许，交互式提示禁用（无法通过 IM 回答）。

### 2. 配置网关

```yaml
# ~/.agentgate/gateway/config.yaml

backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: my-secret-token
    default_window: my-project    # 必须等于 WORK_DIR 的 basename

channels:
  telegram:
    bots:
      - bot_id: my_bot
        bot_token: "123456:ABC-DEF..."
        proxy: "http://127.0.0.1:7897"   # 可选，国内服务器需要

routes:
  - channel: telegram
    bot_id: my_bot
    chat_id: "7003732745"       # 用户或群组的 chat ID
    backend: my-agent
```

**注意：** `default_window` 必须等于 `WORK_DIR` 的目录名。比如 `WORK_DIR=/home/user/my-project`，则 `default_window: my-project`。不匹配 = 输出永远到不了 IM。

### 3. 启动

```bash
# 创建 tmux session（仅 tmux 模式，首次需要）
tmux new-session -d -s agentgate-my-agent -n __main__

# 启动后端
agentgate-backend --name my-agent

# 启动网关
agentgate-gateway --config ~/.agentgate/gateway/config.yaml
```

或者用 systemd（生产环境推荐）：

```bash
# deploy/ 目录下有模板
sudo cp deploy/agentgate-backend@.service /etc/systemd/system/
sudo cp deploy/agentgate-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now agentgate-backend@my-agent
sudo systemctl enable --now agentgate-gateway
```

### 4. 验证

```bash
# 检查后端健康
curl http://127.0.0.1:8903/api/health -H "Authorization: Bearer my-secret-token"

# 检查网关健康
curl http://127.0.0.1:8800/api/health
```

在 Telegram 里发条消息，Agent 收到、处理、回复出现在同一个聊天里。

### 纯 HTTP 模式（不需要 IM）

跳过通道配置，直接用 HTTP 控制 Agent：

```bash
# 给 Agent 发消息
curl -X POST http://localhost:8800/api/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "text": "重构 auth 模块"}'

# 读 Agent 输出
curl "http://localhost:8903/api/output/my-project?since=0" \
  -H "Authorization: Bearer my-secret-token"
```

---

## 路由

消息按 `(通道, Bot, 群组)` 三元组路由——**同一个 Bot** 在**不同群**里可以对接**不同的 Agent**。这是在不增加 bot 数量的情况下扩展项目的关键：

- **不用一个 Agent 一个 Bot。** 一个 bot 覆盖所有项目——放到不同群里就行。
- **群 = 上下文。** 用户在对应项目的群里发消息，路由对用户透明。
- **新项目秒级接入。** 建个群、把 bot 拉进去、加一行路由配置、热加载。不用注册新 bot。

```yaml
routes:
  - channel: feishu
    bot_id: cli_Xxxxx
    chat_id: oc_fish_dev_group
    backend: fish-dev            # → fish 项目的 dev agent

  - channel: feishu
    bot_id: cli_Xxxxx            # 同一个 Bot
    chat_id: oc_trade_dev_group  # 不同的群
    backend: trade-dev           # → trade 项目的 dev agent
```

改完配置热加载，不需要重启：

```bash
kill -HUP $(pidof agentgate-gateway)
# 或者
curl -X POST http://localhost:8800/api/admin/reload
```

IM 连接不断，消息不丢。

---

## Agent 无关

AgentGate 不关心你跑什么 CLI Agent。`AgentDriver` 协议把差异抹平了：

| Agent | 模式 | 输出读取方式 |
|-------|------|-------------|
| Claude Code | tmux | JSONL 文件轮询 |
| Claude Code | subprocess | stream-json stdout |
| OpenCode | tmux | SQLite WAL 只读查询 |
| OpenCode | subprocess | stream-json stdout |

接入新 Agent 类型需要实现 `AgentDriver` 协议，大约 200 行代码，框架不用改。

### 混用 Agent 控制成本

Claude Code 做复杂任务，OpenCode + 本地模型（如 Qwen3.5 通过 llama-server）做日常杂活：

```bash
# 后端 1：Claude Code 做架构工作
AGENTGATE_AGENT_TYPE=claude-code
AGENTGATE_AGENT_MODE=tmux

# 后端 2：OpenCode + 本地 Qwen3.5 做常规任务
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3.5-35B
```

同一个 IM 界面，同一套路由，同一套消息持久化。发消息的人不需要知道对面是哪个 Agent。

---

## 两种模式：tmux 和 subprocess

| | tmux | subprocess |
|---|---|---|
| 执行中纠偏 | 可以，在工具调用间隙 | 排队等当前轮次结束 |
| 实时终端画面 | `tmux attach` | 没有 |
| 需要装 tmux | 是 | 不需要 |
| 输出延迟 | 约 2 秒（文件轮询）| 实时（stdout 流式）|

**tmux 模式**：Agent 运行在持久化的 tmux session 里。你可以 `tmux attach` 实时观察，也可以发消息纠偏。适合重要任务。

**subprocess 模式**：Agent 作为 stdin/stdout 子进程运行。部署更简单，不依赖 tmux，输出实时流式推送。适合常规任务或无人值守自动化。

按 backend 选择。两种模式共享相同的路由、持久化和 API。

---

## 消息持久化

每条消息在处理前先写入 SQLite，入站出站都存。状态只有三种：`pending` → `delivered` 或 `failed`。

```bash
curl -X POST http://localhost:8800/api/messages/query \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "page_size": 20}'
```

完整的审计轨迹，用于调试和追溯。

---

## 高可用

五层防护，各自独立：

| 层级 | 覆盖什么 | 怎么做 |
|------|---------|--------|
| L0 | 进程存活 | systemd `Restart=always` |
| L1 | IM 连接 | 每个通道适配器独立重连，指数退避 |
| L2 | 后端健康 | 持续探活，故障时暂停轮询，恢复后自动继续 |
| L3 | 消息送达 | 先存后发，失败重试，反复失败后告警 |
| L4 | 可观测性 | `/health` 端点暴露全系统状态 |

飞书挂了不影响 Telegram。后端崩了不拖垮网关。Agent 的登录凌晨过期，AgentGate 安静地重试几个小时。期间新来的消息返回 `503`，不会悄悄丢掉。

---

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                       通道网关 (Gateway)                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │   飞书    │  │ Telegram │  │   HTTP   │  │  适配器…    │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘  │
│       │              │             │              │         │
│  ┌────▼──────────────▼─────────────▼──────────────▼──────┐  │
│  │           路由器 (通道, Bot, 群组) → 后端实例            │  │
│  └────┬──────────────────────────────────────────────┬───┘  │
│       │ 注入                               轮询输出   │      │
│  ┌────▼────┐    ┌──────────┐    ┌──────────┐   ┌────▼────┐  │
│  │ 入站处理 │    │ 恢复管理  │    │  SQLite  │   │ 输出轮询 │  │
│  └────┬────┘    └──────────┘    └──────────┘   └────┬────┘  │
└───────┼─────────────────────────────────────────────┼───────┘
        │  HTTP API                        HTTP API   │
┌───────▼─────────────────────────────────────────────▼───────┐
│                  Agent 后端（每实例一个）                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  project-dev  │  │  project-qa  │  │  trade-dev   │       │
│  │  CC + tmux    │  │  OC + tmux   │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**网关**——全局一个。处理通道接入、路由、输出轮询、持久化、崩溃恢复。

**后端**——每个 Agent 一个。管理 Agent 进程、健康检查，通过 HTTP 暴露注入和输出接口。

两层之间走 HTTP。跑在同一台机器上还是分开部署，你自己决定。

---

## CLI 管理工具 (agentgate-ctl)

### 实例生命周期

```bash
# 创建新实例（自动分配端口、生成 token）
agentgate-ctl create my-agent --work-dir ~/my-project --channel telegram --chat-id "7003732745"

# 列出所有实例
agentgate-ctl list

# 状态 / 启动 / 停止 / 重启 / 删除
agentgate-ctl status my-agent
agentgate-ctl stop my-agent
agentgate-ctl start my-agent
agentgate-ctl restart my-agent
agentgate-ctl remove my-agent
```

### Agent 间消息通信

```bash
# 按 backend_id 发消息
agentgate-ctl send my-agent "重构 auth 模块"

# 从 stdin 读（适合长内容）
echo "详细指令..." | agentgate-ctl send my-agent -

# 列出所有可发送的目标
agentgate-ctl send --list

# 查看所有后端健康状态
agentgate-ctl send --status

# 带发送者名称
agentgate-ctl send --from dev my-qa-agent "请验证修复"
```

---

## OpenCode + 本地模型配置

用 OpenCode + 本地模型（如 Qwen3.5 通过 llama-server）作为 agentgate 后端：

### 1. 启动 llama-server

```bash
llama-server -m Qwen3.5-35B-A3B.gguf --port 18090 --ctx-size 262144 --parallel 2
```

### 2. 配置 OpenCode provider

在 `~/.config/opencode/opencode.json` 中添加本地 provider：

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

`limit.context` 设为 `ctx-size / parallel`（如 262144 / 2 = 131072）。

### 3. 创建后端实例

```bash
# .env 配置
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3.5-35B
AGENTGATE_PROCESS_NAME=node
```

AgentGate 自动配置 OpenCode 权限——所有工具允许，交互式提示（AskUser）禁用（无法通过 IM 回答）。

### 4. 项目级模型覆盖

每个后端的工作目录可以放 `opencode.json` 覆盖模型选择：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local/Qwen3.5-35B"
}
```

不同后端可以用不同模型，共享全局 provider 配置。

---

## API

### 网关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 系统状态——通道、后端、待处理消息 |
| POST | `/api/messages/query` | 按条件查询消息历史 |
| POST | `/api/admin/reload` | 热加载配置 |
| POST | `/api/channel/inject` | 直接向后端发消息（绕过 IM） |

### 后端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 后端和 Agent 健康状态 |
| POST | `/api/inject` | 向 Agent 发消息 |
| GET | `/api/output/{window}?since={offset}` | 读取 offset 之后的新输出 |

---

## 排查指南

### Agent 回复没到达 IM

每条日志都包含 trace ID（`[traceID]`），可跨服务关联：

```bash
# 按 backend 查——看某个 agent 的所有活动
journalctl -u agentgate-gateway --since "10 min ago" | grep "backend=my-agent"

# 按 trace ID 查——看一次 poll 的跨服务完整链路
journalctl -u agentgate-gateway -u agentgate-backend@my-agent | grep "abc123def456"

# 按 poll ID 查——看一个 poll 周期内的所有操作
journalctl -u agentgate-gateway | grep "poll=e521e8b3"
```

正常消息有 4 行日志：

1. `[traceID] Polled N new messages from backend=my-agent poll=xxx` — poller 读到了输出
2. `[traceID] Outbound save: msg_id=xxx backend=my-agent poll=xxx → channel:bot chat=xxx` — 已持久化
3. `[traceID] TG outbound [bot]: chat_id=xxx text=xxx` — 开始推送
4. `TG send ok [bot]: elapsed=xxxms` — 推送成功

缺哪一步就是哪里断的：

| 缺失 | 可能原因 |
|------|---------|
| 没有 `Polled` | `default_window` 不匹配（必须等于 WORK_DIR basename）或 session_id 不对 |
| 有 `Polled` 无 `Outbound save` | 被去重拦截（日志 `dedup skip`）或内容类型被过滤（`filtered to 0 text`） |
| 有 `Outbound save` 无 `TG outbound` | push task 失败（日志 `Push task failed`） |
| 有 `TG outbound` 无 `send ok` | Telegram API 失败（超时、限流、代理问题） |

---

## 路线图

- 更多 Agent——Aider、Cursor CLI，或者通过 driver 协议接入自定义 Agent
- 更多通道——Discord、Slack、企业微信
- Web 管理界面——路由配置、健康概览、消息浏览
- 分布式部署——后端是独立的 HTTP 服务，天然支持跨机器部署

---

## 致谢

AgentGate 脱胎于 [ccbot](https://github.com/six-ddc/ccbot)，一个给 Claude Code 做的 Telegram-to-tmux 桥接工具。后端的进程管理、崩溃恢复、健康监控，都是在 ccbot 里做出来验证过的，之后才抽象成现在的 Agent 无关架构。
