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
# 启动后端（管理 Agent 进程）
agentgate-backend --name my-agent --port 8903 --work-dir ~/my-project

# 启动网关（连接 IM 通道到后端）
agentgate-gateway --config config.yaml
```

在 Telegram 群里发条消息，Agent 收到、处理、回复出现在同一个群里。

### 纯 HTTP 模式（不需要 IM）

不用 IM？跳过通道配置，直接用 HTTP 控制 Agent：

```bash
# 给 Agent 发消息
curl -X POST http://localhost:8800/api/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "my-agent", "text": "重构 auth 模块"}'

# 读 Agent 输出
curl "http://localhost:8903/api/output/main?since=0"
```

---

## 路由

消息按 `(通道, Bot, 群组)` 三元组路由。一个 Bot 处理多个项目：

```yaml
routes:
  - channel: feishu
    bot_id: cli_Xxxxx
    chat_id: oc_fish_dev_group
    backend: fish-dev

  - channel: feishu
    bot_id: cli_Xxxxx          # 同一个 Bot
    chat_id: oc_trade_dev_group  # 不同的群
    backend: trade-dev           # 不同的 Agent
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

Claude Code 做复杂任务，OpenCode + qwen-plus 做日常杂活，后者成本大约是前者的 1/20：

```yaml
backends:
  project-dev:
    agent_type: claude-code    # 复杂重构、架构决策
    agent_mode: tmux

  project-qa:
    agent_type: opencode       # 日志分析、跑测试、写样板代码
    agent_mode: subprocess
    model: qwen-plus
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

**tmux 模式**：Agent 运行在持久化的 tmux session 里。你可以 `tmux attach` 实时观察它在干什么，也可以在它工作过程中发消息纠偏——Agent 在两次工具调用之间会看到你的消息。适合重要任务。

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
│  │  CC + tmux    │  │  OC + sub    │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**网关**——全局一个。处理通道接入、路由、输出轮询、持久化、崩溃恢复。

**后端**——每个 Agent 一个。管理 Agent 进程、健康检查，通过 HTTP 暴露注入和输出接口。

两层之间走 HTTP。跑在同一台机器上还是分开部署，你自己决定。

---

## API

### 网关

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 系统状态——通道、后端、待处理消息 |
| POST | `/api/messages/query` | 查询消息历史 |
| POST | `/api/admin/reload` | 热加载配置 |
| POST | `/api/inject` | 直接向后端发消息（绕过 IM） |

### 后端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 后端和 Agent 健康状态 |
| POST | `/api/inject` | 向 Agent 发消息 |
| GET | `/api/output/{window}?since={offset}` | 读取 offset 之后的新输出 |

---

## 路线图

- 更多 Agent——Aider、Cursor CLI，或者通过 driver 协议接入自定义 Agent
- 更多通道——Discord、Slack、企业微信
- Web 管理界面——路由配置、健康概览、消息浏览
- 分布式部署——后端是独立的 HTTP 服务，天然支持跨机器部署

---

## 致谢

AgentGate 脱胎于 [ccbot](https://github.com/six-ddc/ccbot)，一个给 Claude Code 做的 Telegram-to-tmux 桥接工具。后端的进程管理、崩溃恢复、健康监控，都是在 ccbot 里做出来验证过的，之后才抽象成现在的 Agent 无关架构。
