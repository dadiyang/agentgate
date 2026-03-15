# AgentGate

多通道 CLI Agent 网关 —— 让飞书、Telegram、HTTP 连接任意 CLI AI Agent，提供进程管理和高可用保障。

## 5 分钟跑通 Demo

不需要飞书/Telegram 凭证，用内置的 echo backend + HTTP 通道即可验证完整数据流。

### 1. 安装

```bash
# Python >= 3.11
pip install -e .
```

### 2. 启动 echo backend

```bash
# 终端 1：启动一个回声后端（收到消息后原样返回）
echo-backend --port 8950 --token demo-token
```

### 3. 配置并启动 Gateway

```bash
mkdir -p ~/.agentgate/gateway
```

写入最小配置 `~/.agentgate/gateway/config.yaml`：

```yaml
backends:
  demo:
    url: "http://127.0.0.1:8950"
    api_token: "demo-token"
    agent_type: "echo"

port: 8800
```

```bash
# 终端 2：启动网关
agentgate-gateway --config ~/.agentgate/gateway/config.yaml
```

### 4. 发消息、读回复

```bash
# 通过 Gateway HTTP 通道注入消息
curl -s -X POST http://127.0.0.1:8800/api/channel/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "demo", "text": "Hello AgentGate!"}' | python3 -m json.tool

# 等 1 秒让 echo backend 产出回复，然后读取输出
sleep 1
curl -s http://127.0.0.1:8800/api/channel/output/demo?since=0 | python3 -m json.tool
```

你会在 output 响应里看到 `"Echo: Hello AgentGate!"`，说明入站→路由→注入→出站全链路跑通。

### 5. 接入真实 Agent

跑通 demo 后，把 echo backend 替换为真实的 Claude Code backend：

```bash
# 用 agentgate-ctl 创建实例（自动分配端口、生成 token、启动 systemd 服务）
agentgate-ctl create my-agent --workdir /path/to/project

# 绑定 IM 通道（可选）
agentgate-ctl create my-agent \
  --channel feishu --chat-id oc_xxx \
  --workdir /path/to/project
```

---

## 架构

两层分离，可独立运行：

```
IM 通道 (飞书/Telegram/HTTP)
        │
   Channel Gateway        ← 全局一个实例
   (路由 · 轮询 · 格式化 · 持久化)
        │
   Agent Backend ×N       ← 每个项目×角色一个实例
   (进程管理 · 崩溃恢复 · tmux · HTTP API)
        │
   CLI Agent (Claude Code / OpenCode / ...)
```

**入站**：用户在 IM 发消息 → Gateway 按 (channel, bot_id, chat_id) 路由 → 调 Backend HTTP API 注入到 tmux。

**出站**：Gateway 轮询 Backend 增量输出 → 按通道格式化（飞书富文本 / Telegram HTML）→ 长消息自动分割 → 推回 IM。

## API 参考（程序化集成）

AgentGate 提供两层 API：**Gateway API**（面向外部调用者）和 **Backend API**（面向 Gateway 内部或直连场景）。编程集成通常只需要 Gateway API。

### 认证

所有 API 使用 Bearer token 认证：

```
Authorization: Bearer <api_token>
```

- Gateway API token：`config.yaml` 中的 `api_token` 字段（未配置则无需认证）
- Backend API token：每个实例独立，创建时自动生成（见 `~/.agentgate/backends/<name>/.env`）

### 统一响应格式

所有端点返回 JSON，`ok` 字段标识成功/失败：

```json
// 成功
{"ok": true, ...}

// 失败
{"ok": false, "error": "error_code", "msg": "Human readable message"}
```

### Gateway API

Gateway 监听端口由 `config.yaml` 的 `port` 字段决定（默认 8800）。

#### POST /api/channel/inject — 注入消息

通过 HTTP 通道向指定 backend 注入消息，绕过路由直接指定目标。

```bash
curl -X POST http://127.0.0.1:8800/api/channel/inject \
  -H "Authorization: Bearer <gateway_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "backend_id": "my-agent",
    "text": "请帮我修复这个 bug",
    "sender_id": "user-123",
    "sender_name": "张三"
  }'
```

**请求字段**：

| 字段 | 必须 | 说明 |
|------|------|------|
| `backend_id` | 是 | 目标 backend 名称（对应 config.yaml 中的 key） |
| `text` | 是 | 消息文本 |
| `sender_id` | 否 | 发送者 ID（默认 `"api-user"`） |
| `sender_name` | 否 | 发送者显示名（默认 `"HTTP API"`） |

**响应**：

```json
{"ok": true, "message_id": "a1b2c3d4-...", "backend_id": "my-agent"}
```

**错误码**：

| HTTP 状态 | error | 含义 |
|-----------|-------|------|
| 400 | `bad_request` | 缺少 backend_id 或 JSON 格式错误 |
| 401 | `unauthorized` | 缺少 Authorization header |
| 403 | `forbidden` | token 无效 |
| 404 | `backend_not_found` | backend_id 在配置中不存在 |
| 503 | `backend_unhealthy` | backend 健康探测失败，暂时不可用 |

#### GET /api/channel/output/{backend_id} — 读取输出

增量读取 backend 的 agent 输出。用 `since` 参数实现增量轮询。

```bash
# 首次读取（since=0）
curl "http://127.0.0.1:8800/api/channel/output/my-agent?since=0" \
  -H "Authorization: Bearer <gateway_token>"
```

**响应**：

```json
{
  "ok": true,
  "backend_id": "my-agent",
  "window_name": "default",
  "messages": [
    {
      "role": "assistant",
      "text": "我来看一下这个 bug...",
      "content_type": "text",
      "timestamp": "2026-03-15T10:30:00Z"
    }
  ],
  "count": 1,
  "since": 0,
  "next_offset": 4528
}
```

**增量轮询模式**：用响应中的 `next_offset` 作为下次请求的 `since` 值，只获取新增内容。

```python
offset = 0
while True:
    resp = httpx.get(f"{gateway}/api/channel/output/my-agent?since={offset}",
                     headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    for msg in data["messages"]:
        if msg["content_type"] == "text":  # 跳过 thinking 类型
            print(msg["text"])
    offset = data["next_offset"]
    time.sleep(2)
```

#### GET /api/health — 整体健康状态

无需认证。

```json
{
  "status": "ok",
  "uptime_seconds": 3600.5,
  "channels": {
    "feishu": {"status": "connected"},
    "telegram": {"status": "connected"}
  },
  "backends": {
    "my-agent": {
      "status": "healthy",
      "url": "http://127.0.0.1:8903",
      "last_check": "2026-03-15T10:30:00",
      "last_error": null
    }
  },
  "pending_inbound": 0,
  "pending_outbound": 0
}
```

#### POST /api/messages/query — 消息查询

查询持久化的历史消息。

```bash
curl -X POST http://127.0.0.1:8800/api/messages/query \
  -H "Authorization: Bearer <gateway_token>" \
  -H "Content-Type: application/json" \
  -d '{"page": 1, "page_size": 20}'
```

### Backend API（直连）

直接调用 Backend 实例，适用于不经过 Gateway 的场景。端口和 token 见实例的 `.env` 文件。

#### POST /api/inject — 注入消息到 tmux 窗口

```bash
curl -X POST http://127.0.0.1:8903/api/inject \
  -H "Authorization: Bearer <backend_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "window_name": "__main__",
    "text": "请帮我看一下这个文件",
    "message_id": "unique-msg-001",
    "track_delivery": true
  }'
```

**请求字段**：

| 字段 | 必须 | 说明 |
|------|------|------|
| `window_name` | 是 | tmux 窗口名称 |
| `text` | 是 | 注入的文本 |
| `message_id` | 否 | 幂等键，相同 ID 不会重复注入 |
| `track_delivery` | 否 | 是否跟踪送达（默认 true） |

**响应**：

```json
{
  "ok": true,
  "delivery_id": "d-abc123",
  "window_id": "@1",
  "message_id": "unique-msg-001",
  "msg": "Sent to pane successfully"
}
```

**幂等响应**（重复 message_id）：

```json
{
  "ok": true,
  "duplicate": true,
  "message_id": "unique-msg-001",
  "msg": "Duplicate message_id — already injected"
}
```

**错误码**：

| HTTP 状态 | error | 含义 |
|-----------|-------|------|
| 400 | `bad_request` | 缺少 window_name/text 或 JSON 格式错误 |
| 404 | `window_not_found` | 指定的窗口不存在 |
| 500 | `inject_failed` | tmux 注入失败 |

#### GET /api/output/{window_name} — 读取输出

```bash
curl "http://127.0.0.1:8903/api/output/__main__?since=0" \
  -H "Authorization: Bearer <backend_token>"
```

**响应**：

```json
{
  "ok": true,
  "window_name": "__main__",
  "window_id": "@1",
  "messages": [
    {
      "role": "assistant",
      "text": "好的，我来看一下...",
      "content_type": "text",
      "timestamp": "2026-03-15T10:30:00Z"
    }
  ],
  "count": 1,
  "since": 0,
  "next_offset": 4528
}
```

#### POST /api/window — 创建新窗口

```bash
curl -X POST http://127.0.0.1:8903/api/window \
  -H "Authorization: Bearer <backend_token>" \
  -H "Content-Type: application/json" \
  -d '{"work_dir": "/path/to/project", "window_name": "my-task", "start_claude": true}'
```

**响应**：

```json
{"ok": true, "window_name": "my-task", "window_id": "@2", "work_dir": "/path/to/project", "msg": "..."}
```

#### POST /api/confirm_processed — 确认消息已处理

```bash
curl -X POST http://127.0.0.1:8903/api/confirm_processed \
  -H "Authorization: Bearer <backend_token>" \
  -H "Content-Type: application/json" \
  -d '{"message_ids": ["unique-msg-001", "unique-msg-002"]}'
```

**响应**：

```json
{"ok": true, "confirmed": 2, "message_ids": ["unique-msg-001", "unique-msg-002"]}
```

#### GET /api/unprocessed — 查询未确认消息

```json
{
  "ok": true,
  "count": 1,
  "unprocessed": [
    {
      "message_id": "unique-msg-003",
      "window_name": "__main__",
      "window_id": "@1",
      "text_hint": "请帮我看一下...",
      "injected_at": "2026-03-15T10:29:00Z"
    }
  ]
}
```

#### GET /api/health — 实例健康

```json
{
  "status": "ok",
  "windows": [
    {
      "window_id": "@1",
      "window_name": "__main__",
      "pane_command": "claude",
      "session_id": "abc123",
      "pending_deliveries": 0
    }
  ],
  "uptime_seconds": 3600,
  "watchdog_enabled": true,
  "window_health": {"@1": {"status": "ok"}}
}
```

### 典型集成流程

```
1. POST /api/channel/inject     → 发消息，拿到 message_id
2. 轮询 GET /api/channel/output → 用 next_offset 增量读取
3. 从 messages 中过滤 content_type="text" 的条目
4. 检查 GET /api/health          → 确认 backend 状态正常
```

---

## 模块说明

```
src/
├── agentgate_backend/     # Agent 后端（每实例独立进程）
│   ├── main.py            # CLI 入口 (click)
│   ├── config.py          # pydantic-settings 配置，AGENTGATE_ 前缀环境变量
│   ├── inject_server.py   # HTTP API（inject/output/health/window）
│   ├── self_monitor.py    # 进程监控 + 崩溃恢复 + 指数退避
│   ├── tmux_manager.py    # tmux session/window 管理
│   ├── session.py         # Claude Code session 跟踪
│   ├── heartbeat.py       # 心跳上报
│   ├── alerts.py          # 告警（Telegram）
│   └── ...
├── agentgate_gateway/     # 通道网关（全局单实例）
│   ├── main.py            # CLI 入口
│   ├── config.py          # 网关配置模型（YAML → pydantic）
│   ├── router.py          # (channel, bot_id, chat_id) → backend 路由
│   ├── output_poller.py   # 定时拉取 backend 增量输出
│   ├── inbound_handler.py # 入站消息处理
│   ├── formatter.py       # 按通道格式化输出
│   ├── splitter.py        # 长消息分割
│   ├── health_prober.py   # backend 健康探测
│   ├── db.py              # 消息持久化 (SQLite)
│   ├── adapters/          # 通道适配器
│   │   ├── feishu.py      # 飞书（lark-oapi WebSocket 长连接）
│   │   └── telegram.py    # Telegram（python-telegram-bot）
│   └── ...
├── agentgate_ctl/         # 管理 CLI
│   └── main.py            # agentgate-ctl (create/list/status/start/stop/remove)
├── echo_backend/          # 测试用回声后端（echo-backend CLI）
│   └── main.py
```

## 安装与配置

### 安装

```bash
# Python >= 3.11
pip install -e .

# 开发依赖
pip install -e ".[dev]"
```

安装后可用的命令行工具：`agentgate-gateway`、`agentgate-backend`、`agentgate-ctl`、`echo-backend`。

### 运行时目录

```
~/.agentgate/
├── gateway/
│   ├── config.yaml        # 网关配置（通道凭证 + 路由表 + 后端列表）
│   └── messages.db        # 消息持久化
├── backends/
│   └── <name>/
│       ├── .env           # 实例环境变量
│       ├── state.json     # 运行状态
│       └── session_map.json
└── heartbeat/
    └── <name>.json        # 心跳文件
```

### Gateway 配置

编辑 `~/.agentgate/gateway/config.yaml`：

```yaml
# 通道凭证
channels:
  feishu:
    app_id: "cli_xxx"
    app_secret: "xxx"
  telegram:
    proxy: "http://127.0.0.1:7897"   # 可选，国内服务器需要
    bots:
      - bot_token: "123456:ABC..."
        bot_id: "my_bot"

# 后端实例（由 agentgate-ctl create 自动管理）
backends:
  my-agent:
    url: "http://127.0.0.1:8903"
    api_token: "my-agent-xxxx"
    agent_type: "claude-code"        # 或 "echo" 用于测试

# 路由：(channel, bot_id, chat_id) → backend
routes:
  - channel: feishu
    bot_id: "cli_xxx"
    chat_id: "oc_xxx"
    backend: my-agent
  - channel: telegram
    bot_id: "my_bot"
    chat_id: "-100123456"
    backend: my-agent

# 可选
api_token: "gateway-secret"    # Gateway API 认证 token
port: 8800                     # Gateway HTTP API 端口
poll_interval: 2.0             # 输出轮询间隔（秒）
probe_interval: 30.0           # 健康探测间隔（秒）
```

### 部署（systemd）

```bash
# 安装 unit 文件
sudo cp deploy/agentgate-gateway.service /etc/systemd/system/
sudo cp deploy/agentgate-backend@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Backend 使用 systemd 模板实例：`agentgate-backend@<name>.service`，从 `~/.agentgate/backends/<name>/.env` 读取配置。

推荐使用 `agentgate-ctl` 管理实例，它会自动处理配置文件、systemd 服务和 Gateway 路由。

## agentgate-ctl 用法

```bash
# 创建实例（自动分配端口、生成 token、启动服务、注册路由）
agentgate-ctl create my-agent \
  --channel feishu --chat-id oc_xxx \
  --workdir /path/to/project

# 创建 HTTP-only 实例（不绑定 IM 通道）
agentgate-ctl create api-agent --workdir /path/to/project

# 只创建配置，不启动服务
agentgate-ctl create my-agent --channel telegram --chat-id -100123 --no-start

# 列出所有实例
agentgate-ctl list

# 查看实例详情
agentgate-ctl status my-agent

# 启动 / 停止 / 重启
agentgate-ctl start my-agent
agentgate-ctl stop my-agent
agentgate-ctl restart my-agent

# 删除实例（停服务 + kill tmux + 清路由配置）
agentgate-ctl remove my-agent
agentgate-ctl remove my-agent -y   # 跳过确认
```

## 高可用

| 层级 | 关注点 | 机制 |
|------|--------|------|
| L0 | 进程存活 | systemd `Restart=always` |
| L1 | 通道连接 | 适配器独立重连 + 指数退避，永不熔断（代理恢复后自动重连） |
| L2 | Backend 可用性 | Gateway 健康探测，连续失败标记 unhealthy，恢复后自动激活 |
| L3 | 消息送达 | inject 后确认，超时重试（出站失败 >=15 次标记永久失败，不再重试） |
| L4 | 整体可观测 | `/api/health` 端点暴露所有组件状态 |

每个 Telegram bot 是独立的 adapter，单个 bot 断连不影响其他 bot。Gateway 重启后从持久化的 poll offset 继续轮询，不重推历史消息。
