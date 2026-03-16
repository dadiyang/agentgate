# agentgate 工程教训 — 2026-03-16

## [2026-03-16] [agentgate] — output_poller 窗口名硬编码

**发现**：output_poller 两处 `/api/output/main` 硬编码，导致非 "main" 窗口的 backend 无法收到轮询输出。

**上下文**：agentgate backend 的窗口名由 `work_dir.name` 派生（如 `/tmp/agentgate-dev` → 窗口名 `agentgate-dev`）。output_poller 却固定请求 `/api/output/main`，从不实际获取到任何消息。

**教训**：轮询 URL 必须从 backend 配置读 `default_window`，不能写死：
```python
window = getattr(backend, "default_window", None) or backend.get("default_window", "main")
resp = await self._http.get(f"{url}/api/output/{window}?since={offset}", ...)
```
并在 gateway `config.yaml` 各 backend 加 `default_window: <窗口名>`。

---

## [2026-03-16] [agentgate] — editable install 改代码不生效

**发现**：修改了 `output_poller.py`，重启 gateway systemd 服务后问题依然存在，日志显示旧代码路径。

**根因**：Python editable install 有时使用 `.pyc` 缓存文件，而非最新源代码。systemd restart 不清理 `__pycache__`。

**教训**：改动核心模块后怀疑代码没生效时，先删缓存再重启：
```bash
find /home/irons/agentgate_team/agentgate/src -name '__pycache__' -exec rm -rf {} + 2>/dev/null; true
sudo systemctl restart agentgate-gateway.service
```

---

## [2026-03-16] [agentgate] — SQLite 列改名不停服风险

**发现**：`group_id` 全局重命名为 `chat_id` 时，需要同步修改 SQLite DB 的列名，否则新代码写旧列名导致字段不存在错误。

**教训**：列改名必须在服务停止后执行（避免写锁冲突）：
```bash
sudo systemctl stop agentgate-gateway.service
sqlite3 ~/.agentgate/gateway/messages.db \
  "ALTER TABLE inbound_messages RENAME COLUMN group_id TO chat_id;
   ALTER TABLE outbound_messages RENAME COLUMN group_id TO chat_id;"
sudo systemctl start agentgate-gateway.service
```
需要 SQLite 3.25+（2018年发布，现代系统均满足）。

---

## [2026-03-16] [agentgate] — 多 Telegram bot adapter 路由

**发现**：gateway 需要支持多个 Telegram bot（每个角色一个 bot），output_poller 推送时需要路由到正确 bot。

**解法**：adapter key 用 `{channel}:{bot_id}`，查找时先精确后降级：
```python
# 注册：
adapters[f"telegram:{bot_id}"] = TelegramAdapter(bot_token=..., bot_id_override=bot_id)

# 路由：
adapter = adapters.get(f"{channel_type}:{bot_id}") or adapters.get(channel_type)
```
`output_poller._push_to_channel` 需要接收 `bot_id` 参数（来自 router.reverse_lookup 返回的 binding）。

单 bot 旧配置兼容：只注册 `adapters["telegram"]`，降级查找自动命中。

---

## [2026-03-16] [agentgate] — Click CLI 多别名 option

**发现**：`agentgate-ctl create` 只有 `--workdir`，`setup_project.py` 调用时用了 `--work-dir`，导致参数不识别。

**教训**：Click 支持一个 option 有多个名字，直接加第二个名字即可：
```python
@click.option("--workdir", "--work-dir", default=None, ...)
```
不需要两个 option，变量名取第一个名字去掉 `--` 并把 `-` 换成 `_`（`workdir`）。

---

## [2026-03-16] [agentgate] — 后端类型抽象兼容模式

**发现**：ccbot 和 agentgate 用不同字段名存 URL/instance/token，`send-to`/`restart-role` 等工具要同时支持两种格式。

**解法**：helper 函数读新字段，fallback 到旧字段，调用方不感知差异：
```python
def get_backend_url(cfg: dict) -> str:
    return cfg.get("backend_url") or cfg.get("ccbot_url", "?")

def get_backend_instance(cfg: dict) -> str:
    return cfg.get("backend_instance") or cfg.get("ccbot_instance", "")
```
`roles.yaml` 逐步迁移：已迁移的用新字段，未迁移的继续用旧字段，两者都工作。

---

## [2026-03-16] [agentgate] — agentgate-ctl 可选路由参数

**发现**：`agentgate-ctl create` 要求 `--channel` 和 `--group-id` 必填，但创建 HTTP-only backend（无 IM 路由）时不需要这两个参数。

**教训**：CLI 参数应按实际业务需求设可选性：
- `--channel` 可选，不填则只创建 backend，不注册路由
- `--chat-id` 只在 `--channel` 指定时才 required（程序内验证，不用 click required=True）
- 增加 `--no-start` flag：只生成配置文件，不启动 systemd（适合 setup_project.py 批量创建后统一启动）

---

## [2026-03-16] [agentgate] — send-to 经由 gateway inject

**发现**：agentgate 模式的 send-to 不能直连 backend（没有 api_token），需要经由 gateway 的 `/api/channel/inject` 接口。

**实现**：
```python
def send_inject_via_gateway(gateway_url, backend_id, message):
    api_url = gateway_url.rstrip("/") + "/api/channel/inject"
    payload = json.dumps({"backend_id": backend_id, "text": message}).encode()
    # No auth header — gateway is internal, backend_id is the routing key
```
`roles.yaml` 在项目根级加 `gateway_url`，角色只需 `backend_type: agentgate` + `backend_id`，不需要 api_token。
