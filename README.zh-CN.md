# AgentGate

**生产级多通道 CLI Agent 网关**

在飞书或 Telegram 里跟你的 CLI Agent 对话。看它在干什么，随时纠偏，崩溃了自动恢复。

[English](README.md)

---

## 问题在哪

你用 tmux 跑 Claude Code。tmux 是一个终端复用器——简单说，它让你的进程在后台持续运行，即使你关掉终端或断开 SSH 也不会丢。

这解决了"进程不丢"的问题。但没解决"人不在电脑前"的问题。

午饭时想看一眼 Agent 跑到哪了，你得掏手机、开 SSH 客户端、连服务器、`tmux attach`，在巴掌大的屏幕上看一堆代码输出。想发一句"别改那个目录"？在手机键盘上对着 SSH 终端打字，体验很糟。

Agent 一直在跑。问题是你没法方便地跟它沟通。

还有几件事会反复困扰你：

- **Agent 挂了，进程还在。** OAuth 登录过期、上下文窗口撑满、API 限流。tmux 窗口还开着，Agent 却卡在登录提示上。你几个小时后回来才发现。
- **四个 Agent，四个 session。** 两个项目各跑两个 Agent，每次想看状态都要 SSH 进去来回 attach、detach。
- **滚动缓冲区早就没了。** 昨天 Agent 做了什么决策？为什么选了那个方案？终端不会帮你记。

AgentGate 把 IM 接在这一切前面。在飞书群里发条消息，Agent 就收到了。Agent 有输出，你的聊天里就能看到。Agent 凌晨三点崩了，AgentGate 自动拉起来，你收到一条通知。

---

## 执行中纠偏

这是 AgentGate 用 tmux 的核心原因。

AI Agent 会自主决策——读哪个文件、改哪行代码、采取什么方案。有时候它会走错方向。正常情况下你只能等它做完（或者 kill 掉），然后重来。用 AgentGate 的 tmux 模式，你在 Agent 工作过程中发一条消息，它会在两次工具调用之间看到你的纠正：

```
你：   把 auth 模块重构为 JWT
Agent: [第 1 步完成... 第 2 步完成... 第 3 步执行中...]
你：   等下——保留 session token 给旧客户端做降级
Agent: 收到，调整方案...
       [按修正后的方向继续]
```

原理是 tmux 允许 AgentGate 往 Agent 的终端缓冲区写入内容。Agent 完成一次工具调用后检查输入时，就会看到你的消息。跟你亲自在键盘上打字没有区别。

subprocess 模式（stdin/stdout，不用 tmux）做不到这一点。stream-json 协议是严格的一问一答，Agent 忙的时候发过去的消息只能排队等当前轮次结束。这不是 AgentGate 的限制，是协议本身的设计。这个领域最主流的工具也有同样的约束，源码注释写得很明确："do NOT send to agent stdin yet"。

### 实时观测

`tmux attach` 到 Agent 的 session，你看到的就是 Agent 看到的——在读什么文件、调用什么工具、做了什么判断。30 分钟的重构任务，随时瞄一眼确认方向，不用等到最后才发现跑偏了。

### 直接干预

tmux session 就是一个普通的终端。在同一个工作目录下开新 pane，跑 `git diff`，手动改个文件，执行诊断命令。你和 Agent 共享同一个工作环境。tmux 里发生的事情也会推送到 IM——团队能看到完整过程。

---

## 路由

消息按 `(通道, Bot, 群组)` 三元组路由。一个 Bot 处理多个项目——`fish` 群的消息到 `fish-dev`，`trade` 群的消息到 `trade-dev`。每个 Agent 实例完全隔离。

```yaml
routes:
  - channel: feishu
    bot_id: cli_Xxxxx
    chat_id: oc_fish_dev_group
    backend_id: fish-dev

  - channel: feishu
    bot_id: cli_Xxxxx          # 同一个 Bot
    chat_id: oc_trade_dev_group  # 不同的群
    backend_id: trade-dev        # 不同的 Agent
```

两个项目，四个 Agent，一个 IM 入口。改完配置直接热加载：

```bash
kill -HUP $(pidof agentgate-gateway)
# 或者
curl -X POST http://localhost:8800/api/admin/reload
```

IM 连接不断，消息不丢。

---

## 混用 Agent，控制成本

如果你同时在用 Claude Code 和 OpenCode（或者其他接了国产模型的工具），两边可能各有一套操作方式。AgentGate 不关心后端跑的是什么 Agent——`AgentDriver` 协议把差异抹平了。

常见的搭配：复杂任务用 Claude Code，日常杂活用 OpenCode + qwen-plus，后者成本大约是前者的 1/20。

```yaml
backends:
  fish-dev:
    agent_type: claude-code    # 复杂重构、架构决策
    agent_mode: tmux

  fish-qa:
    agent_type: opencode       # 日志分析、跑测试、写样板代码
    agent_mode: subprocess
    model: qwen-plus
```

同一个 IM 界面，同一套路由，同一套消息持久化。发消息的人不需要知道对面是哪个 Agent。

内置驱动：

| Agent | 模式 | 输出读取方式 |
|-------|------|-------------|
| Claude Code | tmux | JSONL 文件轮询 |
| Claude Code | subprocess | stream-json stdout |
| OpenCode | tmux | SQLite WAL 只读查询 |
| OpenCode | subprocess | stream-json stdout |

接入新的 Agent 类型需要实现 `AgentDriver` 协议，大约 200 行代码，框架不用改。

---

## tmux 还是 subprocess

不是所有任务都需要实时监控。两种模式按需选：

| | tmux | subprocess |
|---|---|---|
| 执行中纠偏 | 可以，在工具调用间隙 | 排队等当前轮次结束 |
| 实时终端画面 | `tmux attach` | 没有 |
| 需要装 tmux | 是 | 不需要 |
| 输出延迟 | 约 2 秒（文件轮询）| 实时（stdout 流式）|

按 backend 选择。重要的重构用 tmux，跑测试或生成样板代码用 subprocess 就够了。

---

## 消息持久化

每条消息在处理前先写入 SQLite，入站出站都存。状态只有三种：`pending` → `delivered` 或 `failed`。

```bash
curl -X POST http://localhost:8800/api/messages/query \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "fish-dev", "page_size": 20}'
```

Agent 收到了什么、输出了什么、什么时候投递失败，都能查到。

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
│  │  fish-dev     │  │  fish-qa     │  │  trade-dev   │       │
│  │  CC + tmux    │  │  OC + sub    │  │  CC + tmux   │       │
│  │  :8903        │  │  :8904       │  │  :8905       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

**网关**——全局一个。处理通道接入、路由、输出轮询、持久化、崩溃恢复。

**后端**——每个 Agent 一个。管理 Agent 进程、健康检查，通过 HTTP 暴露注入和输出接口。

两层之间走 HTTP。跑在同一台机器上还是分开部署，你自己决定。

---

## 快速开始

### 前置条件

- Python 3.11+
- tmux（如果用 tmux 模式）
- CLI Agent（Claude Code、OpenCode 等）

### 安装

```bash
git clone https://github.com/anthropics/agentgate.git
cd agentgate
pip install .
```

### 配置

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

### 启动

```bash
# 启动后端
agentgate-backend --instance-dir ~/.agentgate/backends/my-agent \
  --agent-type claude-code --agent-mode tmux \
  --work-dir ~/my-project

# 启动网关
agentgate-gateway --config config.yaml
```

在 Telegram 群里发条消息，Agent 收到、处理、回复出现在同一个群里。

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
