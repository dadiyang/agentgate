---
name: agentgate-usage
description: AgentGate 多通道 CLI Agent 网关的使用指南。覆盖实例创建管理（agentgate-ctl create/send/list/status）、gateway 配置（backends/channels/routes）、.env 配置项、OpenCode + 本地模型（llama-server）接入、Qoder agent 支持、DingTalk 通道配置、agent 间消息通信、消息丢失排查（4 步日志链路 + trace_id/poll_id 关联）。当需要创建 agentgate backend 实例、配置 gateway 路由、接入新 agent（claude-code/opencode/qoder）、配置 DingTalk 通道、发送 agent 间消息、排查消息未送达、或操作 agentgate-ctl 命令时触发此技能。
---

# AgentGate 使用指南

## 架构

```
IM / HTTP → Gateway (路由+持久化+轮询) → Backend (进程管理+健康监控) → CLI Agent
```

- **Gateway**：全局一个。通道接入、(channel,bot,chat) 三元组路由、output 轮询、SQLite 持久化
- **Backend**：每个 agent 一个。管理 agent 进程，暴露 inject/output HTTP API
- 两层走 HTTP，可同机或分离部署

## 实例管理

```bash
# Telegram channel
agentgate-ctl create my-agent --work-dir ~/project --channel telegram --chat-id "123"

# DingTalk channel (requires --bot-id)
agentgate-ctl create my-dingtalk --work-dir ~/project --channel dingtalk --chat-id "conversation_id" --bot-id "client_id"

# Qoder agent
agentgate-ctl create my-qoder --work-dir ~/qoder-project --agent-type qoder

agentgate-ctl list / status <name> / start / stop / restart / remove <name>
```

### Agent 间消息

```bash
agentgate-ctl send <backend_id> "message"     # 发消息
agentgate-ctl send <backend_id> -             # stdin
agentgate-ctl send --list                     # 列出目标
agentgate-ctl send --status                   # 健康概览
```

## 配置

### .env 必填项（`~/.agentgate/backends/<name>/.env`）

`AGENTGATE_NAME`、`AGENTGATE_PORT`（= `HTTP_PORT`，必须一致）、`AGENTGATE_API_TOKEN`、`AGENTGATE_WORK_DIR`、`AGENTGATE_TMUX_SESSION_NAME`（= `agentgate-<name>`）

可选：`AGENTGATE_AGENT_TYPE`（claude-code|opencode|qoder）、`AGENTGATE_AGENT_MODE`（tmux|subprocess）、`AGENTGATE_OPENCODE_MODEL`、`AGENTGATE_PROCESS_NAME`（见下）

**`AGENTGATE_PROCESS_NAME`**：SelfMonitor 用此值判断 agent 是否存活，必须匹配 tmux 报告的前台进程名（`pane_current_command`）。不匹配会导致误判死亡并反复重启。按 agent_type 自动推导默认值（CC=`claude`，OC=`node`，Qoder=`qodercli`），通常不需要手动设。原生编译的 OpenCode 二进制需要覆盖为 `opencode`。验证命令：`tmux list-windows -F '#{window_name} #{pane_current_command}'`

### gateway config（`~/.agentgate/gateway/config.yaml`）

```yaml
backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: <same-as-env>
    agent_type: claude-code               # claude-code|opencode|qoder
    default_window: <WORK_DIR-basename>   # ⚠️ 最常出错：必须 = WORK_DIR 的 basename

channels:
  telegram:
    bots:
      - bot_id: my_bot
        bot_token: "123456:ABC-DEF..."

  dingtalk:
    bots:
      - client_id: your_client_id
        client_secret: your_client_secret
        bot_id: your_bot_id

routes:
  - channel: telegram
    bot_id: my_bot
    chat_id: "7003732745"
    backend: my-agent
```

热加载：`kill -HUP $(pidof agentgate-gateway)` 或 `POST /api/admin/reload`

热加载：`kill -HUP $(pidof agentgate-gateway)` 或 `POST /api/admin/reload`

## 启动后验证（连 IM 前必做）

配置错误会导致 token 浪费（每条消息触发无效处理）或重启风暴（SelfMonitor 误判死亡）。**先验证再接入 IM 流量。**

```bash
# 1. health API
curl http://127.0.0.1:<PORT>/api/health -H "Authorization: Bearer <TOKEN>"

# 2. 进程名匹配检查（tmux 模式）
tmux list-windows -t agentgate-<name> -F '#{window_name} #{pane_current_command}'
# 正确：pane_current_command 与 PROCESS_NAME 配置一致（CC=claude, OC npm=node, OC native=opencode）
# 错误：显示 bash（agent 没启动）或名字不匹配（SelfMonitor 会反复重启）

# 3. gateway 健康
curl http://127.0.0.1:8800/api/health
```

**发现问题 → 先停 backend 再修**：`systemctl stop agentgate-backend@<name>`。不停就修 = IM 消息持续进来持续消耗 token。

## OpenCode + 本地模型

详细配置见 [references/opencode-local-model.md](references/opencode-local-model.md)。

要点：全局 `~/.config/opencode/opencode.json` 加 provider，`limit.context` = ctx-size/parallel，`.env` 设 `AGENT_TYPE=opencode`。`PROCESS_NAME` 默认 `node`（npm 安装），原生二进制需覆盖为 `opencode`。AgentGate 自动 patch `question=deny`。

## 消息丢失排查

```bash
journalctl -u agentgate-gateway --since "10 min ago" | grep "backend=<id>"
```

正常链路 4 步（每步带 `[traceID]`）：

1. `Polled N new messages from backend=xxx poll=yyy`
2. `Outbound save: msg_id=xxx backend=xxx poll=yyy → channel:bot chat=xxx`
3. `TG outbound [bot]: chat_id=xxx text=xxx`
4. `TG send ok [bot]: elapsed=xxxms`

| 缺失 | 查什么 |
|------|--------|
| 无 Polled | default_window 不匹配 WORK_DIR basename |
| Polled 无 Outbound | 日志搜 `dedup skip` 或 `filtered to 0 text` |
| Outbound 无 TG outbound | 日志搜 `Push task failed` |
| TG outbound 无 send ok | TG API 超时/代理问题 |

跨服务关联：`grep "<traceID>"` 串联 gateway + backend 日志；`grep "poll=<id>"` 看单次 poll 周期。

## API

| 端点 | 说明 |
|------|------|
| `GET /api/health` | 健康状态（gateway 或 backend） |
| `POST /api/channel/inject` | 向 backend 发消息（gateway） |
| `POST /api/inject` | 向 agent 注入消息（backend） |
| `GET /api/output/{window}?since={offset}` | 读增量输出（backend） |
| `POST /api/messages/query` | 查询消息历史（gateway） |
| `POST /api/admin/reload` | 热加载配置（gateway） |
