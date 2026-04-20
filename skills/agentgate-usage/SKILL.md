---
name: agentgate-usage
description: AgentGate 多通道 CLI Agent 网关的使用指南。覆盖新实例接入 SOP（agentgate-ctl create、CC Stop hook 注册、gateway 路由配置、启动验证）、.env 配置项、OpenCode + 本地模型接入、Qoder agent 支持、DingTalk 通道配置、agent 间消息通信、消息丢失排查（4 步日志链路 + trace_id/poll_id 关联）、常见报错（session_map 未写入、output poller 404、No route matched、端口冲突）。当需要创建 agentgate backend 实例、配置 gateway 路由、接入新 agent（claude-code/opencode/qoder）、配置 DingTalk 通道、发送 agent 间消息、排查消息未送达、或操作 agentgate-ctl 命令时触发此技能。
---

# AgentGate 使用指南

## 架构

```
IM / HTTP → Gateway (路由+持久化+轮询) → Backend (进程管理+健康监控) → CLI Agent
```

- **Gateway**：全局一个。通道接入、(channel,bot,chat) 三元组路由、output 轮询、SQLite 持久化
- **Backend**：每个 agent 一个。管理 agent 进程，暴露 inject/output HTTP API
- 两层走 HTTP，可同机或分离部署

---

## 新实例接入（SOP）

全新机器按此顺序操作：

1. `agentgate-ctl create` 创建实例（见[实例管理](#实例管理)）
2. **注册 CC Stop hook**（新机器必做，否则 session_map 不写入，见 [CC Stop Hook](#cc-stop-hook)）
3. 编辑 `~/.agentgate/gateway/config.yaml` 加 backend + route（见[配置](#配置)）
4. 启动：`sudo systemctl enable --now agentgate-backend@<name>`（首次需先建 tmux session，见[配置](#配置)）
5. 热加载 gateway：`kill -HUP $(pidof agentgate-gateway)`
6. 连 IM 前先验证（见[启动后验证](#启动后验证)）

> 不用 agentgate-ctl 时：`mkdir -p ~/.agentgate/backends/<name>` 并手写 .env（见[配置 → .env](#env)）

---

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
agentgate-ctl send <backend_id> "message"   # 发消息
agentgate-ctl send <backend_id> -           # stdin
agentgate-ctl send --list                   # 列出目标
agentgate-ctl send --status                 # 健康概览
```

---

## 配置

### .env

路径：`~/.agentgate/backends/<name>/.env`

| 字段 | 说明 |
|------|------|
| `AGENTGATE_NAME` | 实例名，与目录名一致 |
| `AGENTGATE_PORT` | HTTP 端口（必须与 `HTTP_PORT` 相同） |
| `AGENTGATE_HTTP_PORT` | 同上，历史原因两个都要填 |
| `AGENTGATE_API_TOKEN` | Bearer token，gateway config 中一致 |
| `AGENTGATE_WORK_DIR` | agent 工作目录，basename 决定 default_window |
| `AGENTGATE_TMUX_SESSION_NAME` | 固定格式 `agentgate-<name>` |
| `AGENTGATE_CLAUDE_COMMAND` | 启动命令，默认 `claude`，CC 用 `claude --dangerously-skip-permissions` |
| `AGENTGATE_AGENT_TYPE` | `claude-code`（默认）\| `opencode` \| `qoder` |
| `AGENTGATE_AGENT_MODE` | `tmux`（默认）\| `subprocess` |
| `AGENTGATE_PROCESS_NAME` | SelfMonitor 进程名检测，见下方说明 |
| `AGENTGATE_OPENCODE_MODEL` | OpenCode 专用，如 `dashscope/qwen-plus` |

查已用端口：`grep AGENTGATE_PORT ~/.agentgate/backends/*/.env | sort -t= -k2 -n`

**`AGENTGATE_PROCESS_NAME`**：SelfMonitor 用此值判断 agent 是否存活，必须匹配 `pane_current_command`。默认按 agent_type 推导（CC=`claude`，OC=`node`，Qoder=`qodercli`）。原生编译的 OpenCode 二进制需覆盖为 `opencode`。验证：`tmux list-windows -F '#{window_name} #{pane_current_command}'`

### gateway config

路径：`~/.agentgate/gateway/config.yaml`

```yaml
backends:
  my-agent:
    url: http://127.0.0.1:8903
    api_token: <same-as-env>
    agent_type: claude-code               # claude-code|opencode|qoder
    default_window: <WORK_DIR-basename>   # ⚠️ 必须 = WORK_DIR 的 basename

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
    backend: my-agent                     # 字段名是 backend，不是 backend_id
```

热加载：`kill -HUP $(pidof agentgate-gateway)` 或 `POST /api/admin/reload`

**`default_window` = `WORK_DIR` basename**（头号出错点）：

| WORK_DIR | default_window |
|----------|----------------|
| `/home/user/my-project` | `my-project` |
| `/home/user/team/coord` | `coord` |

不匹配 → output poller 持续 404 → agent 有输出但 IM 收不到。

**首次启动前需建 tmux session**（backend 只创建窗口，不创建 session）：

```bash
tmux new-session -d -s agentgate-<name> -n __main__
```

---

## CC Stop Hook

> **新机器必做。** Stop hook 缺失时 CC 完成回复后不写 session_map.json，backend 找不到 JSONL 文件，消息链路静默断裂。

### 部署

```bash
cp /path/to/agentgate/deploy/agentgate-session-hook.py ~/.agentgate/agentgate-session-hook.py
chmod +x ~/.agentgate/agentgate-session-hook.py
```

### 注册到 ~/.claude/settings.json

```json
"hooks": {
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python3 /home/<user>/.agentgate/agentgate-session-hook.py",
          "timeout": 5
        }
      ]
    }
  ]
}
```

### 验证

触发一次 CC 回复后：

```bash
cat ~/.agentgate/backends/<name>/session_map.json   # 有内容 = hook 正常
journalctl -u agentgate-gateway -f | grep Polled    # 有输出 = 链路通
```

---

## 启动后验证

连 IM 前必做，避免 token 浪费或 SelfMonitor 重启风暴。

```bash
# 1. backend health API
curl http://127.0.0.1:<PORT>/api/health -H "Authorization: Bearer <TOKEN>"

# 2. 进程名匹配（tmux 模式）
tmux list-windows -t agentgate-<name> -F '#{window_name} #{pane_current_command}'
# pane_current_command 必须与 PROCESS_NAME 一致；显示 bash = agent 未启动

# 3. gateway 健康
curl http://127.0.0.1:8800/api/health
```

**发现问题先停 backend 再修**：`systemctl stop agentgate-backend@<name>`

---

## 消息丢失排查

```bash
journalctl -u agentgate-gateway --since "10 min ago" | grep "backend=<id>"
```

正常链路 4 步（每步带 `[traceID]`）：

1. `Polled N new messages from backend=xxx poll=yyy`
2. `Outbound save: msg_id=xxx backend=xxx → channel:bot chat=xxx`
3. `TG outbound [bot]: chat_id=xxx text=xxx`
4. `TG send ok [bot]: elapsed=xxxms`

| 缺失步骤 | 根因 |
|----------|------|
| 无 Polled | default_window 不匹配 WORK_DIR basename；或 session_map.json 为空（Stop hook 未注册） |
| Polled 无 Outbound | 搜 `dedup skip` 或 `filtered to 0 text` |
| Outbound 无 TG outbound | 搜 `Push task failed` |
| TG outbound 无 send ok | TG API 超时 / 代理问题 |

跨服务关联：`grep "<traceID>"` 串联 gateway + backend 日志；`grep "poll=<id>"` 看单次 poll 周期。

---

## 常见报错

| 症状 | 根因 | 修复 |
|------|------|------|
| `Session file no longer exists for window_id @xxx` | tmux 重启后 window ID 变了 | 重启 backend（自动 re-resolve） |
| output poller 持续 404 | `default_window` ≠ 实际窗口名 | `tmux list-windows` 对比 config，改后热加载 |
| `No route matched` | (channel,bot_id,chat_id) 三元组不一致 | 看日志实际值对比 config；检查 bot_id 是否加引号、`backend` 字段名是否写成了 `backend_id` |
| `Address already in use` | 端口冲突 | `ss -tlnp \| grep <port>` |
| `session not found` | tmux session 不存在 | `tmux new-session -d -s agentgate-<name> -n __main__` |
| session_map.json 为空 / 有回复但 IM 收不到 | Stop hook 未注册 | 执行 [CC Stop Hook](#cc-stop-hook) |
| Agent 卡在 OAuth / SelfMonitor 标记 degraded | CC 登录过期 | `claude /login` |
| SelfMonitor 反复重启 agent | PROCESS_NAME 不匹配 pane_current_command | 检查并更新 .env 中 PROCESS_NAME |
| 飞书 adapter 启动失败 | app_id/secret 错误或走了代理 | 确认凭证 + 飞书需直连 |
| Telegram adapter 失败 | bot_token 错误或代理未配 | config.yaml adapter 级 `proxy` 字段 |
| subprocess 模式重启后无上下文 | 进程被强杀时 session_id 未保存 | 不可恢复，以新 session 启动 |

详细诊断命令见 [references/troubleshooting.md](references/troubleshooting.md)。

---

## OpenCode + 本地模型

详细配置见 [references/opencode-local-model.md](references/opencode-local-model.md)。

要点：全局 `~/.config/opencode/opencode.json` 加 provider，`limit.context` = ctx-size/parallel，`.env` 设 `AGENT_TYPE=opencode`。`PROCESS_NAME` 默认 `node`（npm 安装），原生二进制需覆盖为 `opencode`。AgentGate 自动 patch `question=deny`。

---

## API

| 端点 | 说明 |
|------|------|
| `GET /api/health` | 健康状态（gateway 或 backend） |
| `POST /api/channel/inject` | 向 backend 发消息（gateway） |
| `POST /api/inject` | 向 agent 注入消息（backend） |
| `GET /api/output/{window}?since={offset}` | 读增量输出（backend） |
| `POST /api/messages/query` | 查询消息历史（gateway） |
| `POST /api/admin/reload` | 热加载配置（gateway） |
