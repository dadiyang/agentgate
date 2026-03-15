# AgentGate

多通道 CLI Agent 网关 —— 让飞书、Telegram、HTTP 连接任意 CLI AI Agent，提供进程管理和高可用保障。

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
echo_backend/              # 测试用回声后端
```

## 快速开始

### 安装

```bash
# Python >= 3.11
pip install -e .

# 开发依赖
pip install -e ".[dev]"
```

安装后可用的命令行工具：`agentgate-gateway`、`agentgate-backend`、`agentgate-ctl`。

### 目录结构

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

### 配置 Gateway

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
    agent_type: "claude-code"

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
port: 8800                  # Gateway HTTP API 端口
poll_interval: 2.0          # 输出轮询间隔（秒）
probe_interval: 30.0        # 健康探测间隔（秒）
```

### 部署（systemd）

项目提供了 systemd unit 文件：

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

## Backend HTTP API

所有端点需要 Bearer token 认证（`Authorization: Bearer <api_token>`）。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/inject` | 注入消息到 tmux 窗口 |
| GET | `/api/output/{window}` | 读取增量输出（支持 byte offset） |
| GET | `/api/health` | 实例健康状态 |
| POST | `/api/window` | 创建新 tmux 窗口并启动 Claude Code |
| POST | `/api/confirm_processed` | 确认消息已处理 |
| GET | `/api/unprocessed` | 查询未确认消息 |
| GET | `/api/delivery/{id}` | 查询送达状态 |

## 高可用

| 层级 | 关注点 | 机制 |
|------|--------|------|
| L0 | 进程存活 | systemd `Restart=always` |
| L1 | 通道连接 | 适配器独立重连 + 指数退避 |
| L2 | Backend 可用性 | Gateway 健康探测，连续失败标记 unhealthy |
| L3 | 消息送达 | inject 后确认，超时重试 |
| L4 | 整体可观测 | `/health` 端点暴露所有组件状态 |
