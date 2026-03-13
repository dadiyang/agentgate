# AgentGate 完整实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现完整的 AgentGate 多通道 CLI Agent 网关系统（Gateway + Backend + Echo 测试后端）

**Architecture:** 两层分离——Gateway（全局唯一，端口 8800）负责多通道接入、路由、持久化、输出轮询；Backend（每实例一个，端口 8901+）负责 tmux 进程管理、Claude Code 崩溃恢复。Gateway 通过 HTTP API 与 Backend 通信，通过 SQLite WAL 持久化消息。

**Tech Stack:** Python 3.11+, aiohttp, aiosqlite, lark-oapi, python-telegram-bot, httpx, pyyaml, pydantic, libtmux, haloant_kit

**关键约束:**
- HTTP 只用 GET 和 POST（禁 PUT/DELETE）
- 告警统一用 haloant_kit.telegram.TelegramSender
- persist-before-process（先持久化再处理）
- 3 层幂等（通道 dedup_key → 网关 UUID → backend message_id）

**ccbot 源码位置:** `/home/irons/ccbot/src/ccbot/`
**技术方案:** `/home/irons/agentgate_team/agentgate-shared/api-spec/technical-design.md`
**PRD:** `/home/irons/agentgate_team/agentgate-shared/prd/agentgate-prd.md`

---

## Task 1: 项目脚手架

**Files:**
- Create: `pyproject.toml`
- Create: `src/agentgate_backend/__init__.py`
- Create: `src/agentgate_gateway/__init__.py`
- Create: `src/agentgate_gateway/adapters/__init__.py`
- Create: `echo_backend/__init__.py`
- Create: `tests/test_backend/__init__.py`
- Create: `tests/test_gateway/__init__.py`

**Step 1: 创建 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "agentgate"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    # Backend deps
    "aiohttp>=3.9",
    "aiofiles>=23.0",
    "libtmux>=0.31",
    "haloant_kit",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "python-dotenv>=1.0",
    # Gateway deps
    "aiosqlite>=0.19",
    "lark-oapi>=1.3",
    "python-telegram-bot>=21.0",
    "httpx>=0.27",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-aiohttp>=1.0",
]

[project.scripts]
agentgate-gateway = "agentgate_gateway.main:main"
agentgate-backend = "agentgate_backend.main:main"
echo-backend = "echo_backend.main:main"

[tool.setuptools.packages.find]
where = ["src", "."]
include = ["agentgate_backend*", "agentgate_gateway*", "echo_backend*"]
```

**Step 2: 创建所有 `__init__.py`**

所有 `__init__.py` 为空文件。

**Step 3: 安装为 editable**

Run: `cd /home/irons/agentgate_team/agentgate && pip install -e ".[dev]"`

**Step 4: 验证 import**

Run: `python -c "import agentgate_backend; import agentgate_gateway; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git init
git add pyproject.toml src/ echo_backend/ tests/ CLAUDE.md
git commit -m "chore: project scaffolding with dual-package structure"
```

---

## Task 2: Backend — 直接复用模块（几乎原样 fork）

从 `/home/irons/ccbot/src/ccbot/` 复制以下模块到 `src/agentgate_backend/`，仅做最小改动（import 路径 ccbot → agentgate_backend）：

**Files:**
- Create: `src/agentgate_backend/tmux_manager.py` ← ccbot/tmux_manager.py (457 行)
- Create: `src/agentgate_backend/delivery_tracker.py` ← ccbot/delivery_tracker.py (130 行)
- Create: `src/agentgate_backend/heartbeat.py` ← ccbot/heartbeat.py (60 行)
- Create: `src/agentgate_backend/catchup.py` ← ccbot/catchup.py (160 行)
- Create: `src/agentgate_backend/utils.py` ← ccbot/utils.py (73 行)
- Create: `src/agentgate_backend/alerts.py` ← ccbot/alerts.py (13 行)

**Step 1: 复制并修改 import 路径**

```bash
for f in tmux_manager.py delivery_tracker.py heartbeat.py catchup.py utils.py alerts.py; do
    cp /home/irons/ccbot/src/ccbot/$f src/agentgate_backend/$f
done
# 全局替换 import 路径
sed -i 's/from ccbot\./from agentgate_backend./g' src/agentgate_backend/*.py
sed -i 's/import ccbot\./import agentgate_backend./g' src/agentgate_backend/*.py
```

**Step 2: utils.py 改造**

`ccbot_dir()` 改名为 `agentgate_dir()`，路径从 `~/.ccbot/` 改为 `~/.agentgate/`。

**Step 3: 验证 import**

Run: `python -c "from agentgate_backend.tmux_manager import TmuxManager; from agentgate_backend.delivery_tracker import DeliveryTracker; print('OK')"`

**Step 4: Commit**

```bash
git add src/agentgate_backend/
git commit -m "feat(backend): fork core modules from ccbot (tmux_manager, delivery_tracker, heartbeat, catchup, utils, alerts)"
```

---

## Task 3: Backend — 适配改造模块

从 ccbot fork 并做关键改造的模块。

**Files:**
- Create: `src/agentgate_backend/self_monitor.py` ← ccbot (504 行，去 CC 特定模式)
- Create: `src/agentgate_backend/session.py` ← ccbot (873 行，去 Telegram 绑定)
- Create: `src/agentgate_backend/session_monitor.py` ← ccbot (534 行，去 Telegram 回调)
- Create: `src/agentgate_backend/transcript_parser.py` ← ccbot (719 行，去 Telegram 格式化)
- Create: `src/agentgate_backend/window_recovery.py` ← ccbot (161 行，session 恢复改进)

### self_monitor.py 改造要点

1. 复制 ccbot self_monitor.py
2. `_CLAUDE_ERROR_PATTERNS` → 移入配置，不硬编码
3. `pane_current_command == "claude"` 检测保留但通过 config 的 `process_name` 字段控制
4. 恢复策略改进：优先 `--resume {session_id}`（从 session_map.json），fallback 到 `--continue`（技术方案 §4.5）
5. 删除直接引用 Telegram 的代码
6. `SelfMonitorConfig` 改为从 pydantic model 加载（不再从 env）

### session.py 改造要点

1. 删除 `thread_bindings`（Telegram thread → window 绑定）
2. 删除 `group_chat_ids`（Telegram 群组 ID 列表）
3. 保留 `WindowState`, `window_states`, `user_window_offsets`
4. 保留 session_map.json 读写
5. 保留 state.json 持久化

### session_monitor.py 改造要点

1. 删除 Telegram 回调耦合（`config.show_user_messages` 等）
2. 保留 JSONL 文件监听、byte-offset 追踪、mtime 缓存
3. 新增输出回调签名：`on_new_output(window_name: str, messages: list[dict])`
4. 输出通过 HTTP API `/api/output/{window_name}` 暴露，不直接推送

### transcript_parser.py 改造要点

1. 删除 `expandable_quote` 和所有 Telegram 格式化逻辑
2. 保留 JSONL 解析核心：`parse_line`, `parse_message`, `parse_entries`
3. 输出纯文本 + content_type 标记（text/thinking/tool_use/tool_result）

### window_recovery.py 改造要点

1. `_do_recover` 中的恢复命令改为：
   - 先查 session_map.json 找 session_id → `claude --resume {session_id}`
   - 无记录时 fallback → `claude --continue`
2. 恢复后等待 session_map.json 更新（最多 15 秒），确认进程启动

**Step 1: 复制并改造**

逐文件复制、替换 import 路径、按上述要点改造。

**Step 2: 写测试 — self_monitor 恢复策略**

```python
# tests/test_backend/test_self_monitor.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agentgate_backend.self_monitor import SelfMonitor

@pytest.mark.asyncio
async def test_restart_prefers_resume_over_continue():
    """session_map 有记录时用 --resume，无记录时 fallback 到 --continue"""
    monitor = SelfMonitor(config=...)
    # mock session_map 有记录
    with patch.object(monitor, '_get_session_id', return_value='abc123'):
        cmd = monitor._build_restart_command('fish-dev')
        assert '--resume' in cmd
        assert 'abc123' in cmd

    # mock session_map 无记录
    with patch.object(monitor, '_get_session_id', return_value=None):
        cmd = monitor._build_restart_command('fish-dev')
        assert '--continue' in cmd
```

**Step 3: 运行测试**

Run: `pytest tests/test_backend/test_self_monitor.py -v`

**Step 4: 确保测试通过后 commit**

```bash
git add src/agentgate_backend/ tests/test_backend/
git commit -m "feat(backend): fork and adapt session/monitor/recovery modules from ccbot"
```

---

## Task 4: Backend — 新建模块

**Files:**
- Create: `src/agentgate_backend/config.py` — pydantic 配置
- Create: `src/agentgate_backend/message_store.py` — 已注入消息 ID 记录（幂等）
- Create: `src/agentgate_backend/hook.py` — Claude Code session hook
- Create: `src/agentgate_backend/inject_server.py` — fork + 扩展（confirm_processed, unprocessed）
- Create: `src/agentgate_backend/main.py` — CLI 入口
- Test: `tests/test_backend/test_inject_server.py`
- Test: `tests/test_backend/test_message_store.py`

### config.py

```python
from pathlib import Path
from pydantic_settings import BaseSettings

class BackendConfig(BaseSettings):
    """agentgate-backend 实例配置。"""
    name: str                        # 实例名（如 fish-dev）
    port: int = 8901                 # HTTP API 端口
    api_token: str                   # Bearer token
    work_dir: Path                   # 项目工作目录
    tmux_session: str = "agentgate"  # tmux session 名
    process_name: str = "claude"     # agent 进程名（用于存活检测）
    data_dir: Path = Path.home() / ".agentgate" / "backends"

    # SelfMonitor 参数
    monitor_interval: int = 30       # 秒
    restart_base_delay: int = 5
    restart_max_delay: int = 300
    restart_max_failures: int = 10

    class Config:
        env_prefix = "AGENTGATE_"
        env_file = ".env"

    @property
    def instance_dir(self) -> Path:
        return self.data_dir / self.name
```

### message_store.py

```python
"""已注入消息 ID 记录，保证幂等。内存 + TTL 清理。"""
import time

class MessageStore:
    def __init__(self, ttl: int = 3600):
        self._store: dict[str, float] = {}  # message_id → timestamp
        self._ttl = ttl

    def has(self, message_id: str) -> bool:
        self._cleanup()
        return message_id in self._store

    def add(self, message_id: str) -> None:
        self._store[message_id] = time.time()

    def _cleanup(self) -> None:
        cutoff = time.time() - self._ttl
        self._store = {k: v for k, v in self._store.items() if v > cutoff}
```

### inject_server.py 扩展

在 ccbot 的 inject_server.py 基础上新增 3 个端点：

1. **POST /api/confirm_processed** — 确认消息已被 agent 处理
2. **GET /api/unprocessed** — 查询未处理消息
3. **/api/inject 增加 message_id 幂等**：检查 MessageStore，重复 message_id 返回 200 但不执行 send_keys

```python
# 在 InjectServer 类中新增
async def _handle_confirm_processed(self, request):
    data = await request.json()
    message_ids = data.get("message_ids", [])
    confirmed = 0
    for mid in message_ids:
        if self._message_store.has(mid):
            self._unprocessed.discard(mid)
            confirmed += 1
    return web.json_response({"ok": True, "confirmed": confirmed})

async def _handle_unprocessed(self, request):
    messages = [
        {"message_id": mid, "injected_at": ts, "text_hint": text[:50]}
        for mid, (ts, text) in self._unprocessed_details.items()
    ]
    return web.json_response({"ok": True, "messages": messages})
```

### hook.py

```python
"""Claude Code session hook — 捕获 session_id 写入 session_map.json。"""
import json, sys
from pathlib import Path

def main():
    """作为 Claude Code 的 --hook 调用，更新 session_map.json。"""
    if len(sys.argv) < 2:
        return
    event = json.loads(sys.argv[1])
    session_id = event.get("session_id")
    window_name = event.get("window_name")
    if not session_id or not window_name:
        return
    map_path = Path.home() / ".agentgate" / "session_map.json"
    data = json.loads(map_path.read_text()) if map_path.exists() else {}
    data[window_name] = session_id
    map_path.write_text(json.dumps(data, indent=2))
```

### main.py

```python
"""agentgate-backend CLI 入口。"""
import asyncio
import click
from agentgate_backend.config import BackendConfig

@click.command()
@click.option("--name", required=True, help="实例名称")
@click.option("--port", type=int, default=None, help="HTTP API 端口")
@click.option("--work-dir", type=click.Path(exists=True), default=None)
def main(name, port, work_dir):
    config = BackendConfig(name=name, **({"port": port} if port else {}), **({"work_dir": work_dir} if work_dir else {}))
    asyncio.run(run(config))

async def run(config: BackendConfig):
    # 1. 初始化 TmuxManager
    # 2. 初始化 InjectServer（含 MessageStore）
    # 3. 启动 SelfMonitor
    # 4. 启动 HTTP server
    # 5. 写心跳
    pass
```

**Step 1: 写测试 — message_store 幂等**

```python
# tests/test_backend/test_message_store.py
from agentgate_backend.message_store import MessageStore

def test_add_and_has():
    store = MessageStore(ttl=3600)
    store.add("msg-1")
    assert store.has("msg-1")
    assert not store.has("msg-2")

def test_duplicate_detection():
    store = MessageStore(ttl=3600)
    store.add("msg-1")
    assert store.has("msg-1")  # 第二次检查仍存在
```

**Step 2: 写测试 — inject 幂等**

```python
# tests/test_backend/test_inject_server.py
import pytest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web

# 验证相同 message_id 不重复 send_keys
@pytest.mark.asyncio
async def test_inject_idempotent(aiohttp_client):
    # 第一次注入 → send_keys 被调用
    # 第二次相同 message_id → 返回 200 但 send_keys 不被调用
    pass
```

**Step 3: 实现所有模块**

按上述代码实现 config.py, message_store.py, hook.py, inject_server.py, main.py。

**Step 4: 运行测试**

Run: `pytest tests/test_backend/ -v`

**Step 5: Commit**

```bash
git add src/agentgate_backend/ tests/test_backend/
git commit -m "feat(backend): add config, message store, hook, extended inject server, CLI entry"
```

---

## Task 5: Echo 测试后端

**Files:**
- Create: `echo_backend/__init__.py`
- Create: `echo_backend/main.py`
- Test: `tests/test_echo_backend.py`

### 设计

Echo backend 暴露与 agentgate-backend 完全相同的 HTTP API，但不启动 tmux。收到消息后将 echo 回复存入内存队列，output 端点返回队列内容。

**触发词行为：**
- `[test-thinking]` → 回复中包含 `content_type: "thinking"` 的条目（测试 Gateway 过滤 thinking 块）
- `[test-markdown]` → 回复包含丰富 Markdown（标题、代码块、列表、加粗、链接）
- `[test-long:N]` → 生成约 N 字符的长回复（测试消息分割）
- `[test-delay:Ns]` → 延迟 N 秒后才产出回复（测试轮询等待）
- 其他 → 原样 echo `"Echo: {原文}"`

```python
# echo_backend/main.py
"""Echo 测试后端 — 模拟 agentgate-backend HTTP API，用于网关功能验证。"""
import asyncio
import re
import uuid
import time
import click
from aiohttp import web

class EchoBackend:
    def __init__(self, port: int, api_token: str):
        self._port = port
        self._token = api_token
        self._output_queue: list[dict] = []  # 待返回的输出
        self._message_store: set[str] = set()  # 幂等
        self._unprocessed: dict[str, dict] = {}  # message_id → info

    def _check_auth(self, request) -> bool:
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {self._token}"

    async def handle_inject(self, request):
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        data = await request.json()
        text = data.get("text", "")
        message_id = data.get("message_id", str(uuid.uuid4()))

        # 幂等检查
        if message_id in self._message_store:
            return web.json_response({"ok": True, "delivery_id": "dup", "window_id": "@echo", "msg": "duplicate"})
        self._message_store.add(message_id)
        self._unprocessed[message_id] = {"injected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "text_hint": text[:50]}

        # 生成回复
        asyncio.create_task(self._generate_reply(text, message_id))

        return web.json_response({"ok": True, "delivery_id": str(uuid.uuid4())[:12], "window_id": "@echo", "msg": "Sent to echo"})

    async def _generate_reply(self, text: str, message_id: str):
        replies = []

        # 解析触发词
        delay_match = re.search(r'\[test-delay:(\d+)s\]', text)
        if delay_match:
            await asyncio.sleep(int(delay_match.group(1)))

        if '[test-thinking]' in text:
            replies.append({"role": "assistant", "text": "内部思考过程...", "content_type": "thinking", "timestamp": self._now()})
            clean_text = text.replace('[test-thinking]', '').strip()
            replies.append({"role": "assistant", "text": f"Echo: {clean_text}", "content_type": "text", "timestamp": self._now()})

        elif '[test-markdown]' in text:
            md = "# Echo 回复\n\n**加粗文本** 和 `内联代码`\n\n```python\ndef hello():\n    print('world')\n```\n\n- 列表项 1\n- 列表项 2\n\n> 引用块"
            replies.append({"role": "assistant", "text": md, "content_type": "text", "timestamp": self._now()})

        elif (long_match := re.search(r'\[test-long:(\d+)\]', text)):
            n = int(long_match.group(1))
            long_text = "这是一段很长的测试文本。" * (n // 10 + 1)
            replies.append({"role": "assistant", "text": long_text[:n], "content_type": "text", "timestamp": self._now()})

        else:
            replies.append({"role": "assistant", "text": f"Echo: {text}", "content_type": "text", "timestamp": self._now()})

        self._output_queue.extend(replies)
        self._unprocessed.pop(message_id, None)  # 标记已处理

    async def handle_output(self, request):
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        since = int(request.query.get("since", "0"))
        messages = self._output_queue[since:]
        return web.json_response({
            "ok": True,
            "window_name": "echo",
            "messages": messages,
            "count": len(messages),
            "since": since,
            "next_offset": len(self._output_queue),
        })

    async def handle_health(self, request):
        return web.json_response({
            "status": "ok",
            "windows": [{"window_id": "@echo", "window_name": "echo", "pane_command": "echo", "pending_deliveries": 0}],
            "uptime_seconds": int(time.time()),
            "watchdog_enabled": False,
            "window_health": {"@echo": {"status": "ok", "detail": "echo backend running"}},
        })

    async def handle_confirm_processed(self, request):
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        data = await request.json()
        message_ids = data.get("message_ids", [])
        confirmed = sum(1 for mid in message_ids if mid in self._message_store)
        return web.json_response({"ok": True, "confirmed": confirmed})

    async def handle_unprocessed(self, request):
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        messages = [{"message_id": mid, **info} for mid, info in self._unprocessed.items()]
        return web.json_response({"ok": True, "messages": messages})

    async def handle_window(self, request):
        if not self._check_auth(request):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        return web.json_response({"ok": True, "window_name": "echo", "window_id": "@echo", "work_dir": "/tmp"})

    @staticmethod
    def _now():
        return time.strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_app(self):
        app = web.Application()
        app.router.add_post("/api/inject", self.handle_inject)
        app.router.add_get("/api/output/{window_name}", self.handle_output)
        app.router.add_get("/api/health", self.handle_health)
        app.router.add_post("/api/confirm_processed", self.handle_confirm_processed)
        app.router.add_get("/api/unprocessed", self.handle_unprocessed)
        app.router.add_post("/api/window", self.handle_window)
        return app

@click.command()
@click.option("--port", type=int, default=8901)
@click.option("--token", default="echo-test-token")
def main(port, token):
    backend = EchoBackend(port=port, api_token=token)
    app = backend.create_app()
    web.run_app(app, host="127.0.0.1", port=port)

if __name__ == "__main__":
    main()
```

**Step 1: 写测试**

```python
# tests/test_echo_backend.py
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from echo_backend.main import EchoBackend

@pytest.fixture
def echo_app():
    backend = EchoBackend(port=8901, api_token="test-token")
    return backend.create_app()

@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}

@pytest.mark.asyncio
async def test_health(aiohttp_client, echo_app):
    client = await aiohttp_client(echo_app)
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"

@pytest.mark.asyncio
async def test_inject_and_output(aiohttp_client, echo_app, auth_headers):
    client = await aiohttp_client(echo_app)
    # inject
    resp = await client.post("/api/inject", json={"text": "hello", "message_id": "m1"}, headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"]
    # 等待异步生成
    import asyncio; await asyncio.sleep(0.1)
    # output
    resp = await client.get("/api/output/echo?since=0", headers=auth_headers)
    data = await resp.json()
    assert data["count"] >= 1
    assert "Echo: hello" in data["messages"][0]["text"]

@pytest.mark.asyncio
async def test_inject_idempotent(aiohttp_client, echo_app, auth_headers):
    client = await aiohttp_client(echo_app)
    await client.post("/api/inject", json={"text": "hi", "message_id": "dup1"}, headers=auth_headers)
    resp = await client.post("/api/inject", json={"text": "hi", "message_id": "dup1"}, headers=auth_headers)
    data = await resp.json()
    assert data["ok"]
    assert data["delivery_id"] == "dup"

@pytest.mark.asyncio
async def test_thinking_trigger(aiohttp_client, echo_app, auth_headers):
    client = await aiohttp_client(echo_app)
    await client.post("/api/inject", json={"text": "[test-thinking] 分析代码", "message_id": "m2"}, headers=auth_headers)
    import asyncio; await asyncio.sleep(0.1)
    resp = await client.get("/api/output/echo?since=0", headers=auth_headers)
    data = await resp.json()
    types = [m["content_type"] for m in data["messages"]]
    assert "thinking" in types
    assert "text" in types

@pytest.mark.asyncio
async def test_auth_required(aiohttp_client, echo_app):
    client = await aiohttp_client(echo_app)
    resp = await client.post("/api/inject", json={"text": "hi"})
    assert resp.status == 403
```

**Step 2: 运行测试验证失败，然后实现，再验证通过**

Run: `pytest tests/test_echo_backend.py -v`

**Step 3: Commit**

```bash
git add echo_backend/ tests/test_echo_backend.py
git commit -m "feat: add echo test backend with trigger words for QA validation"
```

---

## Task 6: Gateway — 配置 + 数据库 + 路由

**Files:**
- Create: `src/agentgate_gateway/config.py`
- Create: `src/agentgate_gateway/db.py`
- Create: `src/agentgate_gateway/router.py`
- Test: `tests/test_gateway/test_config.py`
- Test: `tests/test_gateway/test_db.py`
- Test: `tests/test_gateway/test_router.py`

### config.py

```python
"""YAML 配置加载 + pydantic 校验。"""
from pathlib import Path
from pydantic import BaseModel
import yaml

class FeishuConfig(BaseModel):
    app_id: str
    app_secret: str

class TelegramConfig(BaseModel):
    bot_token: str
    proxy: str = ""

class ChannelsConfig(BaseModel):
    feishu: FeishuConfig | None = None
    telegram: TelegramConfig | None = None

class BackendConfig(BaseModel):
    url: str
    api_token: str
    agent_type: str = "claude-code"

class RouteConfig(BaseModel):
    channel: str
    bot_id: str
    group_id: str
    backend: str

class AlertsConfig(BaseModel):
    feishu_webhook: str = ""
    telegram_chat_id: str = ""
    telegram_bot_token: str = ""

class GatewayConfig(BaseModel):
    channels: ChannelsConfig = ChannelsConfig()
    backends: dict[str, BackendConfig] = {}
    routes: list[RouteConfig] = []
    alerts: AlertsConfig = AlertsConfig()

    # 运行时参数
    port: int = 8800
    db_path: Path = Path.home() / ".agentgate" / "gateway" / "messages.db"
    poll_interval: float = 2.0
    probe_interval: float = 30.0
    test_mode: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> "GatewayConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
```

### db.py

```python
"""SQLite WAL 消息持久化层。"""
import aiosqlite
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id              TEXT PRIMARY KEY,
    received_at     TEXT NOT NULL,
    delivered_at    TEXT,
    processed_at    TEXT,
    channel_type    TEXT NOT NULL,
    channel_bot_id  TEXT NOT NULL DEFAULT '',
    group_id        TEXT NOT NULL DEFAULT '',
    group_name      TEXT NOT NULL DEFAULT '',
    sender_id       TEXT NOT NULL DEFAULT '',
    sender_name     TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL,
    backend_id      TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    process_status  TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    dedup_key       TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS outbound_messages (
    id              TEXT PRIMARY KEY,
    fetched_at      TEXT NOT NULL,
    pushed_at       TEXT,
    backend_id      TEXT NOT NULL,
    channel_type    TEXT NOT NULL,
    group_id        TEXT NOT NULL,
    group_name      TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL,
    push_status     TEXT NOT NULL DEFAULT 'pending',
    shard_index     INTEGER NOT NULL DEFAULT 1,
    shard_total     INTEGER NOT NULL DEFAULT 1,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    content_hash    TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbound_delivery ON inbound_messages(delivery_status);
CREATE INDEX IF NOT EXISTS idx_inbound_process ON inbound_messages(process_status);
CREATE INDEX IF NOT EXISTS idx_inbound_backend ON inbound_messages(backend_id);
CREATE INDEX IF NOT EXISTS idx_outbound_push ON outbound_messages(push_status);
CREATE INDEX IF NOT EXISTS idx_outbound_backend ON outbound_messages(backend_id);
"""

class MessageDB:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # --- Inbound ---
    async def save_inbound(self, msg: dict) -> None: ...
    async def update_inbound_delivery(self, msg_id: str, status: str, delivered_at: str = None) -> None: ...
    async def update_inbound_process(self, msg_id: str, status: str, processed_at: str = None) -> None: ...
    async def get_pending_inbound(self) -> list[dict]: ...
    async def get_unprocessed_for_backend(self, backend_id: str) -> list[dict]: ...

    # --- Outbound ---
    async def save_outbound(self, msg: dict) -> None: ...
    async def update_outbound_push(self, msg_id: str, status: str, pushed_at: str = None) -> None: ...
    async def get_pending_outbound(self) -> list[dict]: ...
    async def get_failed_outbound(self) -> list[dict]: ...

    # --- Query API (F13) ---
    async def query_messages(self, filters: dict) -> tuple[list[dict], int]: ...

    async def has_dedup_key(self, key: str) -> bool:
        async with self._db.execute("SELECT 1 FROM inbound_messages WHERE dedup_key = ?", (key,)) as cursor:
            return await cursor.fetchone() is not None
```

### router.py

```python
"""路由匹配 — 精确三元组查找。"""
from agentgate_gateway.config import RouteConfig

class Router:
    def __init__(self, routes: list[RouteConfig]):
        self._table: dict[tuple[str, str, str], str] = {}
        self._reverse: dict[str, list[tuple[str, str, str]]] = {}
        for r in routes:
            key = (r.channel, r.bot_id, r.group_id)
            self._table[key] = r.backend
            self._reverse.setdefault(r.backend, []).append(key)

    def match(self, channel: str, bot_id: str, group_id: str) -> str | None:
        return self._table.get((channel, bot_id, group_id))

    def reverse_lookup(self, backend_id: str) -> list[tuple[str, str, str]]:
        return self._reverse.get(backend_id, [])
```

**Step 1: 写测试**

```python
# tests/test_gateway/test_router.py
from agentgate_gateway.router import Router
from agentgate_gateway.config import RouteConfig

def test_exact_match():
    routes = [RouteConfig(channel="feishu", bot_id="bot1", group_id="g1", backend="fish-dev")]
    router = Router(routes)
    assert router.match("feishu", "bot1", "g1") == "fish-dev"
    assert router.match("feishu", "bot1", "g2") is None  # 未配置 → None

def test_reverse_lookup():
    routes = [
        RouteConfig(channel="feishu", bot_id="b", group_id="g1", backend="fish-dev"),
        RouteConfig(channel="telegram", bot_id="b", group_id="g2", backend="fish-dev"),
    ]
    router = Router(routes)
    bindings = router.reverse_lookup("fish-dev")
    assert len(bindings) == 2

def test_no_route_returns_none():
    router = Router([])
    assert router.match("feishu", "x", "y") is None
```

```python
# tests/test_gateway/test_db.py
import pytest, tempfile
from pathlib import Path
from agentgate_gateway.db import MessageDB

@pytest.fixture
async def db(tmp_path):
    d = MessageDB(tmp_path / "test.db")
    await d.init()
    yield d
    await d.close()

@pytest.mark.asyncio
async def test_save_and_query_inbound(db):
    await db.save_inbound({
        "id": "m1", "received_at": "2026-03-13T10:00:00Z",
        "channel_type": "feishu", "content": "hello",
        "dedup_key": "feishu-msg-1",
    })
    pending = await db.get_pending_inbound()
    assert len(pending) == 1
    assert pending[0]["id"] == "m1"

@pytest.mark.asyncio
async def test_dedup_key_unique(db):
    await db.save_inbound({"id": "m1", "received_at": "now", "channel_type": "feishu", "content": "a", "dedup_key": "dup1"})
    # 相同 dedup_key 应被拒绝
    assert await db.has_dedup_key("dup1")
```

**Step 2: 运行测试**

Run: `pytest tests/test_gateway/ -v`

**Step 3: Commit**

```bash
git add src/agentgate_gateway/ tests/test_gateway/
git commit -m "feat(gateway): add config loader, SQLite message DB, and routing engine"
```

---

## Task 7: Gateway — 通道适配器

**Files:**
- Create: `src/agentgate_gateway/adapters/base.py` — ChannelAdapter 基类 + test_disconnect
- Create: `src/agentgate_gateway/adapters/feishu.py` — 飞书长连接适配器
- Create: `src/agentgate_gateway/adapters/telegram.py` — Telegram polling 适配器
- Create: `src/agentgate_gateway/adapters/http.py` — HTTP 透传通道
- Test: `tests/test_gateway/test_adapters.py`

### base.py — 含 Admin API 的 test_disconnect 支持

```python
"""通道适配器基类。"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# 入站消息回调签名
OnMessageCallback = Callable[
    [str, str, str, str, str, str, str, str],  # channel_type, bot_id, group_id, sender_id, sender_name, group_name, text, dedup_key
    Awaitable[None],
]

class ChannelAdapter(ABC):
    def __init__(self, name: str, on_message: OnMessageCallback):
        self.name = name
        self._on_message = on_message
        self._test_disconnected = False  # Admin API 用

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def _real_send_message(self, group_id: str, text: str) -> bool: ...

    @abstractmethod
    def _real_is_connected(self) -> bool: ...

    async def send_message(self, group_id: str, text: str) -> bool:
        if self._test_disconnected:
            logger.warning("Adapter %s: test_disconnected=True, simulating send failure", self.name)
            return False
        return await self._real_send_message(group_id, text)

    def is_connected(self) -> bool:
        if self._test_disconnected:
            return False
        return self._real_is_connected()

    def test_disconnect(self, duration: int = 0):
        self._test_disconnected = True
        if duration > 0:
            asyncio.get_event_loop().call_later(duration, self.test_reconnect)

    def test_reconnect(self):
        self._test_disconnected = False
```

### feishu.py 关键结构

```python
"""飞书适配器 — lark-oapi WebSocket 长连接。"""
import asyncio, json, logging
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
from agentgate_gateway.adapters.base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)

class FeishuAdapter(ChannelAdapter):
    def __init__(self, app_id: str, app_secret: str, on_message: OnMessageCallback):
        super().__init__(name="feishu", on_message=on_message)
        self._app_id = app_id
        self._app_secret = app_secret
        self._client: lark.Client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self._ws_client = None
        self._connected = False
        self._task = None

    async def start(self):
        # 注册消息事件处理器
        event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._handle_message_event).build()
        self._ws_client = lark.ws.Client(self._app_id, self._app_secret, event_handler=event_handler, log_level=lark.LogLevel.WARNING)
        self._task = asyncio.create_task(asyncio.to_thread(self._ws_client.start))
        self._connected = True

    async def stop(self):
        self._connected = False
        # lark ws client 没有 graceful stop，取消 task
        if self._task:
            self._task.cancel()

    def _handle_message_event(self, ctx, conf, event):
        """飞书消息事件回调（在线程中运行）。"""
        msg = event.event.message
        sender = event.event.sender
        if msg.message_type != "text":
            return  # 只处理文本消息
        content = json.loads(msg.content).get("text", "")
        chat_id = msg.chat_id
        # 跨线程调度到 asyncio event loop
        asyncio.run_coroutine_threadsafe(
            self._on_message(
                "feishu", self._app_id, chat_id, sender.sender_id.open_id,
                sender.sender_id.open_id, "", content, msg.message_id,
            ),
            asyncio.get_event_loop(),
        )

    async def _real_send_message(self, group_id: str, text: str) -> bool:
        """使用飞书 REST API 发送文本消息。"""
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(group_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()) \
            .build()
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        if not response.success():
            logger.error("Feishu send failed: %s %s", response.code, response.msg)
            return False
        return True

    def _real_is_connected(self) -> bool:
        return self._connected
```

### telegram.py 关键结构

```python
"""Telegram 适配器 — python-telegram-bot polling。"""
import asyncio, logging, os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from agentgate_gateway.adapters.base import ChannelAdapter, OnMessageCallback

logger = logging.getLogger(__name__)

class TelegramAdapter(ChannelAdapter):
    def __init__(self, bot_token: str, on_message: OnMessageCallback, proxy: str = ""):
        super().__init__(name="telegram", on_message=on_message)
        self._bot_token = bot_token
        builder = ApplicationBuilder().token(bot_token)
        if proxy:
            builder = builder.proxy(proxy).get_updates_proxy(proxy)
        self._app = builder.build()
        self._connected = False
        self._bot_username = ""

    async def start(self):
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        await self._app.initialize()
        me = await self._app.bot.get_me()
        self._bot_username = me.username
        await self._app.start()
        await self._app.updater.start_polling()
        self._connected = True

    async def stop(self):
        self._connected = False
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self._test_disconnected:
            return  # 模拟收不到消息
        msg = update.effective_message
        if not msg or not msg.text:
            return
        chat_id = str(msg.chat_id)
        user = msg.from_user
        await self._on_message(
            "telegram", self._bot_username, chat_id,
            str(user.id), user.full_name or user.username or str(user.id),
            msg.chat.title or "", msg.text, str(update.update_id),
        )

    async def _real_send_message(self, group_id: str, text: str) -> bool:
        try:
            await self._app.bot.send_message(chat_id=int(group_id), text=text, parse_mode="HTML")
            return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e, exc_info=True)
            return False

    def _real_is_connected(self) -> bool:
        return self._connected
```

**Step 1: 写测试（base.py test_disconnect）**

```python
# tests/test_gateway/test_adapters.py
import pytest, asyncio
from agentgate_gateway.adapters.base import ChannelAdapter

class MockAdapter(ChannelAdapter):
    def __init__(self):
        super().__init__(name="mock", on_message=None)
        self._real_connected = True
        self._sent = []

    async def start(self): pass
    async def stop(self): pass
    async def _real_send_message(self, group_id, text):
        self._sent.append((group_id, text))
        return True
    def _real_is_connected(self): return self._real_connected

@pytest.mark.asyncio
async def test_normal_send():
    adapter = MockAdapter()
    assert await adapter.send_message("g1", "hi")
    assert adapter._sent == [("g1", "hi")]

@pytest.mark.asyncio
async def test_test_disconnect_blocks_send():
    adapter = MockAdapter()
    adapter.test_disconnect()
    assert not await adapter.send_message("g1", "hi")
    assert adapter._sent == []
    assert not adapter.is_connected()

@pytest.mark.asyncio
async def test_test_reconnect_restores():
    adapter = MockAdapter()
    adapter.test_disconnect()
    adapter.test_reconnect()
    assert await adapter.send_message("g1", "hi")
    assert adapter.is_connected()
```

**Step 2: 运行测试，实现，commit**

```bash
git add src/agentgate_gateway/adapters/ tests/test_gateway/test_adapters.py
git commit -m "feat(gateway): add channel adapters (feishu, telegram, http) with test_disconnect support"
```

---

## Task 8: Gateway — 格式化 + 消息分割

**Files:**
- Create: `src/agentgate_gateway/formatter.py`
- Create: `src/agentgate_gateway/splitter.py`
- Test: `tests/test_gateway/test_formatter.py`
- Test: `tests/test_gateway/test_splitter.py`

### formatter.py

```python
"""Markdown → 通道格式转换。"""
import re

def to_feishu_rich(text: str) -> str:
    """Markdown → 飞书富文本（简化版，飞书原生支持部分 Markdown）。"""
    # 飞书消息 API 支持 text 类型，直接发送 Markdown 文本
    # 复杂场景后续可扩展为卡片消息
    return text

def to_telegram_html(text: str) -> str:
    """Markdown → Telegram HTML。"""
    # 代码块
    text = re.sub(r'```(\w*)\n(.*?)```', r'<pre>\2</pre>', text, flags=re.DOTALL)
    # 内联代码
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # 加粗
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # 斜体
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    return text

def format_for_channel(channel_type: str, text: str) -> str:
    if channel_type == "feishu":
        return to_feishu_rich(text)
    elif channel_type == "telegram":
        return to_telegram_html(text)
    return text  # HTTP/其他 → 纯文本
```

### splitter.py

```python
"""长消息按通道限制分割。"""

CHANNEL_LIMITS = {
    "feishu": 30000,
    "telegram": 4096,
}
DEFAULT_LIMIT = 30000

def split_message(text: str, channel_type: str) -> list[str]:
    """按通道限制分割消息。返回分片列表。"""
    limit = CHANNEL_LIMITS.get(channel_type, DEFAULT_LIMIT)
    if len(text) <= limit:
        return [text]

    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # 优先在段落边界分割
        cut = text.rfind('\n\n', 0, limit)
        if cut == -1:
            cut = text.rfind('\n', 0, limit)
        if cut == -1:
            cut = text.rfind('。', 0, limit)
        if cut == -1:
            cut = text.rfind('. ', 0, limit)
        if cut == -1:
            cut = limit  # 强制截断
        else:
            cut += 1  # 包含分隔符

        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()

    return parts
```

**Step 1: 写测试**

```python
# tests/test_gateway/test_splitter.py
from agentgate_gateway.splitter import split_message

def test_short_message_no_split():
    assert split_message("hello", "telegram") == ["hello"]

def test_telegram_split_at_4096():
    text = "x" * 8000
    parts = split_message(text, "telegram")
    assert len(parts) >= 2
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == text  # 无丢失

def test_split_prefers_paragraph_boundary():
    text = "段落一\n\n" + "x" * 4000 + "\n\n段落三"
    parts = split_message(text, "telegram")
    assert len(parts) >= 2
    # 应在 \n\n 处分割
```

```python
# tests/test_gateway/test_formatter.py
from agentgate_gateway.formatter import to_telegram_html

def test_bold():
    assert "<b>粗体</b>" in to_telegram_html("**粗体**")

def test_code_block():
    assert "<pre>" in to_telegram_html("```python\nprint(1)\n```")

def test_inline_code():
    assert "<code>x</code>" in to_telegram_html("`x`")
```

**Step 2: 运行测试，实现，commit**

```bash
git add src/agentgate_gateway/formatter.py src/agentgate_gateway/splitter.py tests/test_gateway/
git commit -m "feat(gateway): add message formatter (markdown→html) and smart splitter"
```

---

## Task 9: Gateway — 入站/出站消息处理

**Files:**
- Create: `src/agentgate_gateway/inbound_handler.py`
- Create: `src/agentgate_gateway/output_poller.py`
- Create: `src/agentgate_gateway/outbound_handler.py`
- Test: `tests/test_gateway/test_inbound_handler.py`

### inbound_handler.py

入站消息全链路：通道回调 → 去重 → 持久化 → 路由 → 注入 backend → 更新状态。

```python
"""入站消息处理。"""
import uuid, logging
from datetime import datetime, timezone
import httpx
from agentgate_gateway.db import MessageDB
from agentgate_gateway.router import Router

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT = 30  # 秒
MAX_RETRY = 3
RETRY_DELAYS = [5, 10, 15]  # 指数退避

class InboundHandler:
    def __init__(self, db: MessageDB, router: Router, backends: dict, adapters: dict):
        self._db = db
        self._router = router
        self._backends = backends  # backend_id → BackendConfig
        self._adapters = adapters  # channel_type → ChannelAdapter
        self._http = httpx.AsyncClient(timeout=10)

    async def handle_message(self, channel_type: str, bot_id: str, group_id: str,
                             sender_id: str, sender_name: str, group_name: str,
                             text: str, dedup_key: str):
        """通道适配器的入站回调。"""
        # 1. 幂等检查
        if await self._db.has_dedup_key(dedup_key):
            logger.info("Duplicate message: dedup_key=%s", dedup_key)
            return

        # 2. 路由匹配
        backend_id = self._router.match(channel_type, bot_id, group_id)
        if not backend_id:
            logger.debug("No route for (%s, %s, %s), ignoring", channel_type, bot_id, group_id)
            return  # AC-10: 静默忽略

        # 3. 持久化（persist-before-process）
        msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.save_inbound({
            "id": msg_id,
            "received_at": now,
            "channel_type": channel_type,
            "channel_bot_id": bot_id,
            "group_id": group_id,
            "group_name": group_name,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": text,
            "backend_id": backend_id,
            "dedup_key": dedup_key,
        })

        # 4. 注入 backend（带重试）
        await self._inject_with_retry(msg_id, backend_id, text, sender_name, channel_type, group_id)

    async def _inject_with_retry(self, msg_id, backend_id, text, sender_name, channel_type, group_id):
        backend = self._backends.get(backend_id)
        if not backend:
            logger.error("Backend %s not configured", backend_id)
            return

        for attempt in range(MAX_RETRY):
            try:
                resp = await self._http.post(
                    f"{backend.url}/api/inject",
                    json={"text": text, "message_id": msg_id, "sender_name": sender_name},
                    headers={"Authorization": f"Bearer {backend.api_token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        now = datetime.now(timezone.utc).isoformat()
                        await self._db.update_inbound_delivery(msg_id, "delivered", now)
                        return
                logger.warning("Inject attempt %d failed: %s", attempt + 1, resp.text)
            except Exception as e:
                logger.error("Inject attempt %d error: %s", attempt + 1, e, exc_info=True)

            if attempt < MAX_RETRY - 1:
                import asyncio
                await asyncio.sleep(RETRY_DELAYS[attempt])

        # 全部重试失败
        await self._db.update_inbound_delivery(msg_id, "failed", error_message="3 retries exhausted")
        # AC-29: 通知用户
        adapter = self._adapters.get(channel_type)
        if adapter:
            await adapter.send_message(group_id, "⚠️ 消息暂时无法处理，系统正在恢复中。请稍后重试或联系管理员。")
```

### output_poller.py

```python
"""出站输出轮询器 — 定时拉取各 backend 输出。"""
import asyncio, hashlib, logging, uuid
from datetime import datetime, timezone
import httpx

logger = logging.getLogger(__name__)

class OutputPoller:
    def __init__(self, db, router, backends, adapters, formatter, splitter, poll_interval=2.0):
        self._db = db
        self._router = router
        self._backends = backends
        self._adapters = adapters
        self._formatter = formatter
        self._splitter = splitter
        self._poll_interval = poll_interval
        self._offsets: dict[str, int] = {}  # backend_id → byte offset
        self._http = httpx.AsyncClient(timeout=10)
        self._running = True

    async def run(self):
        while self._running:
            for backend_id, backend in self._backends.items():
                if backend.status == "unhealthy":
                    continue
                try:
                    await self._poll_backend(backend_id, backend)
                except Exception as e:
                    logger.error("Poll %s failed: %s", backend_id, e, exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _poll_backend(self, backend_id, backend):
        offset = self._offsets.get(backend_id, 0)
        resp = await self._http.get(
            f"{backend.url}/api/output/main?since={offset}",
            headers={"Authorization": f"Bearer {backend.api_token}"},
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("ok") or data.get("count", 0) == 0:
            return

        self._offsets[backend_id] = data.get("next_offset", offset)

        # 过滤 thinking 块（AC-12）
        text_messages = [m for m in data["messages"] if m.get("content_type") == "text"]
        if not text_messages:
            return

        # 合并文本
        combined = "\n\n".join(m["text"] for m in text_messages)

        # 反向路由找通道
        bindings = self._router.reverse_lookup(backend_id)
        for channel_type, bot_id, group_id in bindings:
            await self._push_to_channel(backend_id, channel_type, group_id, combined)

    async def _push_to_channel(self, backend_id, channel_type, group_id, text):
        # 格式化
        formatted = self._formatter(channel_type, text)
        # 分割
        parts = self._splitter(formatted, channel_type)

        for i, part in enumerate(parts):
            content_hash = hashlib.sha256(f"{backend_id}:{part}".encode()).hexdigest()
            msg_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()

            # 持久化
            await self._db.save_outbound({
                "id": msg_id,
                "fetched_at": now,
                "backend_id": backend_id,
                "channel_type": channel_type,
                "group_id": group_id,
                "content": part,
                "shard_index": i + 1,
                "shard_total": len(parts),
                "content_hash": content_hash,
            })

            # 推送
            adapter = self._adapters.get(channel_type)
            if adapter:
                success = await adapter.send_message(group_id, part)
                if success:
                    pushed_at = datetime.now(timezone.utc).isoformat()
                    await self._db.update_outbound_push(msg_id, "pushed", pushed_at)
                else:
                    await self._db.update_outbound_push(msg_id, "failed", error_message="send_message returned False")

    def stop(self):
        self._running = False
```

**Step 1: 写测试（inbound 重试 + 幂等）**

```python
# tests/test_gateway/test_inbound_handler.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from agentgate_gateway.inbound_handler import InboundHandler

@pytest.mark.asyncio
async def test_duplicate_message_ignored():
    db = AsyncMock()
    db.has_dedup_key = AsyncMock(return_value=True)
    handler = InboundHandler(db=db, router=MagicMock(), backends={}, adapters={})
    await handler.handle_message("feishu", "b", "g", "s", "name", "group", "hi", "dup1")
    db.save_inbound.assert_not_called()  # 重复消息不保存

@pytest.mark.asyncio
async def test_no_route_silently_ignored():
    db = AsyncMock()
    db.has_dedup_key = AsyncMock(return_value=False)
    router = MagicMock()
    router.match = MagicMock(return_value=None)
    handler = InboundHandler(db=db, router=router, backends={}, adapters={})
    await handler.handle_message("feishu", "b", "g", "s", "name", "group", "hi", "k1")
    db.save_inbound.assert_not_called()  # 无路由不保存
```

**Step 2: 运行测试，实现，commit**

```bash
git add src/agentgate_gateway/inbound_handler.py src/agentgate_gateway/output_poller.py src/agentgate_gateway/outbound_handler.py tests/test_gateway/
git commit -m "feat(gateway): add inbound handler, output poller, and outbound push pipeline"
```

---

## Task 10: Gateway — 健康探测 + 崩溃恢复 + 告警

**Files:**
- Create: `src/agentgate_gateway/health_prober.py`
- Create: `src/agentgate_gateway/recovery.py`
- Create: `src/agentgate_gateway/alert_manager.py`
- Test: `tests/test_gateway/test_health_prober.py`

### health_prober.py

```python
"""Backend 健康探测 — L2 可用性检测。"""
import asyncio, logging
import httpx

logger = logging.getLogger(__name__)

class BackendState:
    def __init__(self, url: str, api_token: str):
        self.url = url
        self.api_token = api_token
        self.status = "unknown"  # healthy / unhealthy / unknown
        self.fail_count = 0
        self.last_check = None
        self.last_error = None

class HealthProber:
    CONSECUTIVE_FAIL = 3
    TIMEOUT = 10

    def __init__(self, backends: dict[str, BackendState], on_recovered, on_unhealthy,
                 probe_interval=30, probe_interval_low=60):
        self._backends = backends
        self._on_recovered = on_recovered
        self._on_unhealthy = on_unhealthy
        self._probe_interval = probe_interval
        self._probe_interval_low = probe_interval_low
        self._http = httpx.AsyncClient(timeout=self.TIMEOUT)
        self._running = True

    async def run(self):
        while self._running:
            for backend_id, state in self._backends.items():
                await self._probe(backend_id, state)
            interval = self._probe_interval
            if any(s.status == "unhealthy" for s in self._backends.values()):
                interval = self._probe_interval_low
            await asyncio.sleep(interval)

    async def _probe(self, backend_id: str, state: BackendState):
        try:
            resp = await self._http.get(
                f"{state.url}/api/health",
                headers={"Authorization": f"Bearer {state.api_token}"},
            )
            if resp.status_code == 200:
                was_unhealthy = state.status == "unhealthy"
                state.status = "healthy"
                state.fail_count = 0
                state.last_error = None
                if was_unhealthy:
                    await self._on_recovered(backend_id)
                return
            state.fail_count += 1
            state.last_error = f"HTTP {resp.status_code}"
        except Exception as e:
            state.fail_count += 1
            state.last_error = str(e)

        if state.fail_count >= self.CONSECUTIVE_FAIL and state.status != "unhealthy":
            state.status = "unhealthy"
            await self._on_unhealthy(backend_id)

    def stop(self):
        self._running = False
```

### recovery.py

```python
"""Gateway 崩溃恢复 — 启动时补偿 + backend 恢复后补偿。"""
import logging
import httpx
from agentgate_gateway.db import MessageDB

logger = logging.getLogger(__name__)

class RecoveryManager:
    def __init__(self, db: MessageDB, backends: dict, adapters: dict, inject_fn, push_fn):
        self._db = db
        self._backends = backends
        self._adapters = adapters
        self._inject = inject_fn
        self._push = push_fn

    async def recover_on_startup(self):
        """Gateway 启动时恢复 pending/failed 消息（PRD F14）。"""
        # 1. pending 入站 → 重新注入
        pending = await self._db.get_pending_inbound()
        logger.info("Recovery: %d pending inbound messages", len(pending))
        for msg in pending:
            await self._inject(msg)

        # 2. pending 出站 → 重新推送
        pending_out = await self._db.get_pending_outbound()
        logger.info("Recovery: %d pending outbound messages", len(pending_out))
        for msg in pending_out:
            await self._push(msg)

        # 3. failed 出站 → 尝试补推
        failed_out = await self._db.get_failed_outbound()
        logger.info("Recovery: %d failed outbound messages to retry", len(failed_out))
        for msg in failed_out:
            await self._push(msg)

    async def on_backend_recovered(self, backend_id: str):
        """Backend 从 unhealthy 恢复后的消息补偿（PRD F07 AC-21/AC-22）。"""
        unprocessed = await self._db.get_unprocessed_for_backend(backend_id)
        logger.info("Backend %s recovered, %d unprocessed messages to reinject", backend_id, len(unprocessed))
        for msg in unprocessed:
            await self._db.update_inbound_process(msg["id"], "reinjected")
            await self._inject(msg)
```

### alert_manager.py

```python
"""告警管理 — Telegram + 飞书 webhook。"""
import logging
from haloant_kit.telegram import TelegramSender

logger = logging.getLogger(__name__)

class AlertManager:
    def __init__(self, config):
        self._tg_sender = None
        if config.telegram_bot_token and config.telegram_chat_id:
            self._tg_sender = TelegramSender(config.telegram_bot_token)
            self._tg_chat_id = config.telegram_chat_id

    async def send(self, alert_type: str, severity: str, detail: str, affected: str = ""):
        text = (
            f"🚨 [AgentGate 告警]\n"
            f"类型：{alert_type}\n"
            f"严重度：{severity}\n"
            f"影响：{affected}\n"
            f"详情：{detail}"
        )
        if self._tg_sender:
            try:
                await self._tg_sender.send(self._tg_chat_id, text)
            except Exception as e:
                logger.error("Alert send failed: %s", e, exc_info=True)
```

**Step 1: 写测试**

```python
# tests/test_gateway/test_health_prober.py
import pytest
from unittest.mock import AsyncMock, patch
from agentgate_gateway.health_prober import HealthProber, BackendState

@pytest.mark.asyncio
async def test_healthy_to_unhealthy_after_3_failures():
    state = BackendState(url="http://localhost:9999", api_token="t")
    state.status = "healthy"
    on_unhealthy = AsyncMock()
    prober = HealthProber({"test": state}, on_recovered=AsyncMock(), on_unhealthy=on_unhealthy)
    # 模拟 3 次连接失败
    with patch.object(prober._http, 'get', side_effect=Exception("refused")):
        for _ in range(3):
            await prober._probe("test", state)
    assert state.status == "unhealthy"
    on_unhealthy.assert_called_once_with("test")

@pytest.mark.asyncio
async def test_unhealthy_to_healthy_triggers_recovery():
    state = BackendState(url="http://localhost:8901", api_token="t")
    state.status = "unhealthy"
    on_recovered = AsyncMock()
    prober = HealthProber({"test": state}, on_recovered=on_recovered, on_unhealthy=AsyncMock())
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    with patch.object(prober._http, 'get', return_value=mock_resp):
        await prober._probe("test", state)
    assert state.status == "healthy"
    on_recovered.assert_called_once_with("test")
```

**Step 2: 运行测试，实现，commit**

```bash
git add src/agentgate_gateway/health_prober.py src/agentgate_gateway/recovery.py src/agentgate_gateway/alert_manager.py tests/test_gateway/
git commit -m "feat(gateway): add health prober, crash recovery, and alert manager"
```

---

## Task 11: Gateway — API 路由 + Admin API + Main

**Files:**
- Create: `src/agentgate_gateway/api.py`
- Create: `src/agentgate_gateway/main.py`
- Test: `tests/test_gateway/test_api.py`

### api.py

```python
"""Gateway HTTP API 路由注册。"""
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

def setup_routes(app: web.Application, gateway):
    """注册所有 Gateway HTTP 路由。"""
    # --- 公共 API ---
    app.router.add_post("/api/channel/inject", gateway.handle_http_inject)
    app.router.add_get("/api/channel/output/{backend_id}", gateway.handle_http_output)
    app.router.add_get("/api/health", gateway.handle_health)
    app.router.add_post("/api/messages/query", gateway.handle_messages_query)

    # --- Admin API（仅 test-mode）---
    if gateway.config.test_mode:
        app.router.add_post("/api/admin/adapter/{name}/disconnect", gateway.handle_admin_disconnect)
        app.router.add_post("/api/admin/adapter/{name}/reconnect", gateway.handle_admin_reconnect)
        app.router.add_get("/api/admin/test-mode", gateway.handle_admin_test_mode)


class GatewayAPI:
    """Gateway API 处理逻辑。"""

    def __init__(self, config, db, router, adapters, backends, inbound_handler, output_poller):
        self.config = config
        self._db = db
        self._router = router
        self._adapters = adapters
        self._backends = backends
        self._inbound = inbound_handler
        self._poller = output_poller

    # --- HTTP Channel ---
    async def handle_http_inject(self, request):
        data = await request.json()
        backend_id = data.get("backend_id")
        if not backend_id:
            return web.json_response({"ok": False, "error": "bad_request", "msg": "backend_id required"}, status=400)
        if backend_id not in self._backends:
            return web.json_response({"ok": False, "error": "backend_not_found", "msg": f"Backend '{backend_id}' not configured"}, status=404)
        backend = self._backends[backend_id]
        if backend.status == "unhealthy":
            return web.json_response({"ok": False, "error": "backend_unhealthy", "msg": f"Backend '{backend_id}' is unhealthy"}, status=503)

        # 作为 HTTP 通道消息处理
        await self._inbound.handle_message(
            "http", "", "", data.get("sender_id", ""), data.get("sender_name", ""),
            "", data.get("text", ""), f"http-{data.get('backend_id')}-{id(data)}",
        )
        return web.json_response({"ok": True, "message_id": "...", "backend_id": backend_id})

    # --- Health ---
    async def handle_health(self, request):
        channels = {}
        for name, adapter in self._adapters.items():
            channels[name] = {
                "status": "connected" if adapter.is_connected() else "disconnected",
            }
        backends = {}
        for bid, state in self._backends.items():
            backends[bid] = {
                "status": state.status,
                "url": state.url,
                "last_check": state.last_check,
            }
            if state.last_error:
                backends[bid]["error"] = state.last_error

        return web.json_response({
            "status": "ok",
            "channels": channels,
            "backends": backends,
            "pending_inbound": len(await self._db.get_pending_inbound()),
            "pending_outbound": len(await self._db.get_pending_outbound()),
        })

    # --- Messages Query (F13) ---
    async def handle_messages_query(self, request):
        filters = await request.json()
        messages, total = await self._db.query_messages(filters)
        return web.json_response({
            "ok": True,
            "total": total,
            "page": filters.get("page", 1),
            "page_size": filters.get("page_size", 50),
            "messages": messages,
        })

    # --- Admin API ---
    async def handle_admin_disconnect(self, request):
        name = request.match_info["name"]
        adapter = self._adapters.get(name)
        if not adapter:
            return web.json_response({"ok": False, "error": "adapter_not_found", "msg": f"Adapter '{name}' not configured"}, status=404)
        data = await request.json() if request.can_read_body else {}
        duration = data.get("duration_seconds", 0)
        adapter.test_disconnect(duration)
        resp = {"ok": True, "adapter": name, "status": "disconnected"}
        if duration > 0:
            resp["auto_reconnect_after"] = duration
        return web.json_response(resp)

    async def handle_admin_reconnect(self, request):
        name = request.match_info["name"]
        adapter = self._adapters.get(name)
        if not adapter:
            return web.json_response({"ok": False, "error": "adapter_not_found"}, status=404)
        adapter.test_reconnect()
        return web.json_response({"ok": True, "adapter": name, "status": "connected"})

    async def handle_admin_test_mode(self, request):
        adapters = {}
        for name, adapter in self._adapters.items():
            adapters[name] = {
                "status": "connected" if adapter.is_connected() else "disconnected",
                "test_disconnected": adapter._test_disconnected,
            }
        return web.json_response({"ok": True, "test_mode": True, "adapters": adapters})
```

### main.py

```python
"""agentgate-gateway CLI 入口。"""
import asyncio, logging, signal
from pathlib import Path
import click
from aiohttp import web

from agentgate_gateway.config import GatewayConfig
from agentgate_gateway.db import MessageDB
from agentgate_gateway.router import Router
from agentgate_gateway.adapters.feishu import FeishuAdapter
from agentgate_gateway.adapters.telegram import TelegramAdapter
from agentgate_gateway.adapters.base import ChannelAdapter
from agentgate_gateway.health_prober import HealthProber, BackendState
from agentgate_gateway.inbound_handler import InboundHandler
from agentgate_gateway.output_poller import OutputPoller
from agentgate_gateway.recovery import RecoveryManager
from agentgate_gateway.alert_manager import AlertManager
from agentgate_gateway.formatter import format_for_channel
from agentgate_gateway.splitter import split_message
from agentgate_gateway.api import GatewayAPI, setup_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("agentgate-gateway")

@click.command()
@click.option("--config", "config_path", required=True, type=click.Path(exists=True), help="YAML 配置文件路径")
@click.option("--port", type=int, default=None, help="HTTP 端口（覆盖配置文件）")
@click.option("--test-mode", is_flag=True, default=False, help="启用 Admin API（测试用）")
def main(config_path, port, test_mode):
    config = GatewayConfig.from_yaml(Path(config_path))
    if port:
        config.port = port
    if test_mode:
        config.test_mode = True
    asyncio.run(run(config))

async def run(config: GatewayConfig):
    # 1. 初始化 DB
    db = MessageDB(config.db_path)
    await db.init()

    # 2. 构建路由
    router = Router(config.routes)

    # 3. 构建 backend 状态
    backend_states = {
        bid: BackendState(url=bc.url, api_token=bc.api_token)
        for bid, bc in config.backends.items()
    }

    # 4. 构建入站回调
    adapters: dict[str, ChannelAdapter] = {}
    inbound = InboundHandler(db, router, config.backends, adapters)

    async def on_message(*args):
        await inbound.handle_message(*args)

    # 5. 构建通道适配器
    if config.channels.feishu:
        adapters["feishu"] = FeishuAdapter(
            config.channels.feishu.app_id,
            config.channels.feishu.app_secret,
            on_message,
        )
    if config.channels.telegram:
        adapters["telegram"] = TelegramAdapter(
            config.channels.telegram.bot_token,
            on_message,
            proxy=config.channels.telegram.proxy,
        )

    # 更新 inbound handler 的 adapters 引用
    inbound._adapters = adapters

    # 6. 输出轮询
    poller = OutputPoller(db, router, backend_states, adapters, format_for_channel, split_message, config.poll_interval)

    # 7. 健康探测
    alert_mgr = AlertManager(config.alerts)
    recovery = RecoveryManager(db, backend_states, adapters, inbound._inject_with_retry, None)

    async def on_recovered(bid):
        await recovery.on_backend_recovered(bid)

    async def on_unhealthy(bid):
        await alert_mgr.send("backend_unhealthy", "CRITICAL", f"Backend {bid} unreachable", bid)

    prober = HealthProber(backend_states, on_recovered, on_unhealthy, config.probe_interval)

    # 8. 启动时恢复
    await recovery.recover_on_startup()

    # 9. HTTP 服务
    gateway_api = GatewayAPI(config, db, router, adapters, backend_states, inbound, poller)
    app = web.Application()
    setup_routes(app, gateway_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info("Gateway started on port %d (test_mode=%s)", config.port, config.test_mode)

    # 10. 启动所有异步任务
    tasks = []
    for name, adapter in adapters.items():
        tasks.append(asyncio.create_task(adapter.start(), name=f"adapter-{name}"))
    tasks.append(asyncio.create_task(poller.run(), name="output-poller"))
    tasks.append(asyncio.create_task(prober.run(), name="health-prober"))

    # 等待退出信号
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    poller.stop()
    prober.stop()
    for adapter in adapters.values():
        await adapter.stop()
    for task in tasks:
        task.cancel()
    await runner.cleanup()
    await db.close()

if __name__ == "__main__":
    main()
```

**Step 1: 写测试（Admin API）**

```python
# tests/test_gateway/test_api.py
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from unittest.mock import AsyncMock, MagicMock
from agentgate_gateway.api import GatewayAPI, setup_routes

@pytest.fixture
def mock_gateway():
    config = MagicMock()
    config.test_mode = True
    adapter = MagicMock()
    adapter.is_connected.return_value = True
    adapter._test_disconnected = False
    gw = GatewayAPI(config, MagicMock(), MagicMock(), {"feishu": adapter}, {}, MagicMock(), MagicMock())
    return gw, adapter

@pytest.fixture
def app_with_admin(mock_gateway):
    gw, _ = mock_gateway
    app = web.Application()
    setup_routes(app, gw)
    return app

@pytest.mark.asyncio
async def test_admin_disconnect(aiohttp_client, app_with_admin, mock_gateway):
    _, adapter = mock_gateway
    client = await aiohttp_client(app_with_admin)
    resp = await client.post("/api/admin/adapter/feishu/disconnect", json={"duration_seconds": 10})
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"]
    assert data["status"] == "disconnected"
    adapter.test_disconnect.assert_called_once_with(10)

@pytest.mark.asyncio
async def test_admin_reconnect(aiohttp_client, app_with_admin, mock_gateway):
    _, adapter = mock_gateway
    client = await aiohttp_client(app_with_admin)
    resp = await client.post("/api/admin/adapter/feishu/reconnect")
    assert resp.status == 200
    adapter.test_reconnect.assert_called_once()

@pytest.mark.asyncio
async def test_admin_adapter_not_found(aiohttp_client, app_with_admin):
    client = await aiohttp_client(app_with_admin)
    resp = await client.post("/api/admin/adapter/nonexistent/disconnect", json={})
    assert resp.status == 404

@pytest.mark.asyncio
async def test_health_endpoint(aiohttp_client, app_with_admin, mock_gateway):
    gw, _ = mock_gateway
    gw._db.get_pending_inbound = AsyncMock(return_value=[])
    gw._db.get_pending_outbound = AsyncMock(return_value=[])
    client = await aiohttp_client(app_with_admin)
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert "channels" in data
```

**Step 2: 运行测试，实现，commit**

```bash
git add src/agentgate_gateway/api.py src/agentgate_gateway/main.py tests/test_gateway/
git commit -m "feat(gateway): add HTTP API with admin endpoints and CLI entry point"
```

---

## Task 12: 部署文件

**Files:**
- Create: `deploy/agentgate-gateway.service`
- Create: `deploy/agentgate-backend@.service`
- Create: `deploy/config.yaml.template`

### systemd service files

```ini
# deploy/agentgate-gateway.service
[Unit]
Description=AgentGate Channel Gateway
After=network.target

[Service]
Type=simple
User=irons
ExecStart=/home/irons/.local/bin/agentgate-gateway --config /home/irons/.agentgate/gateway/config.yaml
Restart=always
RestartSec=3
Environment=HTTPS_PROXY=http://127.0.0.1:7897

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/agentgate-backend@.service
[Unit]
Description=AgentGate Backend %i
After=network.target

[Service]
Type=simple
User=irons
ExecStart=/home/irons/.local/bin/agentgate-backend --name %i
Restart=always
RestartSec=3
EnvironmentFile=/home/irons/.agentgate/backends/%i/.env

[Install]
WantedBy=multi-user.target
```

### config.yaml template

```yaml
# deploy/config.yaml.template
# AgentGate 网关配置模板
# 复制到 ~/.agentgate/gateway/config.yaml 并填入真实凭证

channels:
  feishu:
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
  telegram:
    bot_token: "${TELEGRAM_BOT_TOKEN}"
    proxy: "http://127.0.0.1:7897"  # 中国服务器必须走代理

backends:
  echo-test:
    url: "http://127.0.0.1:8901"
    api_token: "echo-test-token"
    agent_type: "echo"
  # fish-dev:
  #   url: "http://127.0.0.1:8902"
  #   api_token: "your-token"
  #   agent_type: "claude-code"

routes:
  # Echo 测试路由
  - channel: telegram
    bot_id: "${TELEGRAM_BOT_USERNAME}"
    group_id: "${TELEGRAM_PRIVATE_CHAT_ID}"
    backend: echo-test
  # - channel: feishu
  #   bot_id: "${FEISHU_APP_ID}"
  #   group_id: "oc_xxx"
  #   backend: fish-dev

alerts:
  telegram_chat_id: "${TELEGRAM_PRIVATE_CHAT_ID}"
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
```

**Step 1: 创建文件**

创建上述 3 个文件。

**Step 2: Commit**

```bash
git add deploy/
git commit -m "chore: add systemd service files and config.yaml template"
```

---

## Task 13: 集成冒烟测试

验证全链路可用：echo_backend + gateway + HTTP 通道。

**Step 1: 创建测试配置**

```yaml
# /tmp/agentgate-smoke-config.yaml
channels: {}

backends:
  echo-test:
    url: "http://127.0.0.1:8901"
    api_token: "echo-test-token"

routes: []

port: 8800
test_mode: true
```

**Step 2: 启动 echo backend**

```bash
echo-backend --port 8901 --token echo-test-token &
```

**Step 3: 启动 gateway**

```bash
agentgate-gateway --config /tmp/agentgate-smoke-config.yaml --test-mode &
```

**Step 4: 验证 health**

```bash
curl http://127.0.0.1:8800/api/health
# Expected: {"status": "ok", "backends": {"echo-test": {"status": "healthy", ...}}}
```

**Step 5: 验证 HTTP 通道注入 + 输出**

```bash
# 注入
curl -X POST http://127.0.0.1:8800/api/channel/inject \
  -H "Content-Type: application/json" \
  -d '{"backend_id": "echo-test", "text": "hello from smoke test", "sender_name": "tester"}'
# Expected: {"ok": true, ...}

# 等 1 秒
sleep 1

# 读输出
curl http://127.0.0.1:8800/api/channel/output/echo-test?since=0
# Expected: {"ok": true, "messages": [{"text": "Echo: hello from smoke test", ...}]}
```

**Step 6: 验证 Admin API**

```bash
curl -X GET http://127.0.0.1:8800/api/admin/test-mode
# Expected: {"ok": true, "test_mode": true, ...}
```

**Step 7: 停止进程，commit 测试脚本**

```bash
# 保存为 scripts/smoke_test.sh 方便复用
git add scripts/
git commit -m "test: add integration smoke test script"
```

---

## 实现顺序和依赖

```
Task 1 (scaffolding) ─── 无依赖
    │
    ├── Task 2 (backend 直接 fork)
    │       │
    │       └── Task 3 (backend 适配改造)
    │               │
    │               └── Task 4 (backend 新建模块) ─── 完整 backend
    │
    ├── Task 5 (echo backend) ─── 可与 Task 2-4 并行
    │
    ├── Task 6 (gateway config+db+router) ─── 可与 Task 2-5 并行
    │       │
    │       ├── Task 7 (adapters)
    │       │       │
    │       │       └── Task 8 (formatter+splitter)
    │       │
    │       └── Task 9 (inbound+outbound)
    │               │
    │               └── Task 10 (health+recovery+alerts)
    │                       │
    │                       └── Task 11 (API+main) ─── 完整 gateway
    │
    └── Task 12 (deploy) ─── 可与 Task 2-11 并行
            │
            └── Task 13 (integration) ─── 需要所有 Task 完成
```

---

## AC 覆盖映射

| PRD AC | 实现位置 |
|--------|---------|
| AC-01~03 (飞书接入) | Task 7 FeishuAdapter |
| AC-04~06 (Telegram 接入) | Task 7 TelegramAdapter |
| AC-07~09 (HTTP 通道) | Task 11 GatewayAPI |
| AC-10 (路由精确匹配) | Task 6 Router |
| AC-11 (消息注入) | Task 9 InboundHandler |
| AC-12 (thinking 过滤) | Task 9 OutputPoller |
| AC-13 (长消息分割) | Task 8 splitter |
| AC-14~15 (通道自愈) | Task 7 AdapterManager + Task 10 |
| AC-16~18 (进程管理) | Task 3 SelfMonitor |
| AC-19~20 (崩溃恢复) | Task 3 SelfMonitor + window_recovery |
| AC-21~22 (消息补偿) | Task 10 RecoveryManager |
| AC-23~24 (通道隔离) | Task 7 base.py + Task 11 Admin API |
| AC-25~28 (消息持久化) | Task 6 MessageDB |
| AC-29 (失败通知) | Task 9 InboundHandler |
| AC-30~31 (幂等) | Task 4 MessageStore + Task 6 DB dedup |
| AC-32~33 (健康端点) | Task 11 GatewayAPI.handle_health |
| AC-34~39 (消息查询) | Task 11 GatewayAPI.handle_messages_query |
| AC-40~42 (崩溃恢复) | Task 10 RecoveryManager |
| AC-43 (故障注入) | Task 11 Admin API |
