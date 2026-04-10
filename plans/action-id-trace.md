# Action ID 链路追踪方案

## 目标

在 gateway 的 `messages` 表中添加 `action_id` 字段，用于关联一次 Q&A 的 inbound 和 outbound 消息。

## 现状问题

- `messages` 表只有 `id`（DB 主键）和 `direction`（inbound/outbound）
- inbound 和 outbound 的 `id` 完全独立，无法关联同一 Q&A
- trace_id 为 0，因为 inbound 和 output_poller 各自创建独立 span

## 设计方案

### 字段设计

```sql
messages (
    id TEXT PRIMARY KEY,          -- DB 主键，inbound/outbound 各自独立 UUID
    action_id TEXT,               -- Q&A 关联键，入站出站共享同一个 ID
    direction TEXT,               -- 'inbound' / 'outbound'
    ...
)
```

**示例数据：**
```
inbound:  id="uuid-in-1", action_id="q-001", direction="inbound"    -- 用户问
outbound: id="uuid-out-1", action_id="q-001", direction="outbound"  -- 助手答
```

**查询链路：**
```sql
SELECT * FROM messages WHERE action_id = 'q-001' ORDER BY timestamp;
```

### 数据流

```
gateway inbound:
  1. 生成 action_id = uuid4()
  2. save_inbound(id=uuid-in, action_id=q-001, ...)
  3. POST /api/inject {message_id: q-001}

backend:
  1. 收到 message_id，存到 _unprocessed[message_id]
  2. Claude Code 处理
  3. GET /api/output 返回 {latest_message_id: q-001, messages=[...]}

gateway output:
  1. poll 获取 latest_message_id
  2. save_outbound(id=uuid-out, action_id=q-001, ...)
  3. 推送成功后 → POST /api/confirm_processed {action_ids: [q-001]}
```

## 实现步骤

### 1. DB schema 变更

- `messages` 表加 `action_id TEXT` 字段
- 加索引 `CREATE INDEX idx_msg_action_id ON messages(action_id)`

### 2. inbound_handler 修改

- `handle_message` 生成 `action_id = uuid4()`
- `save_inbound` 存储 `action_id`

### 3. output_poller 修改

- `_poll_backend_inner` 从 backend 响应获取 `latest_message_id`
- `_push_to_channel` 接收 `action_id` 参数并存储
- `_confirm_processed` 按 `action_id` 查询 inbound 并更新状态

### 4. backend 配合（可选，当前版本不强制）

- `/api/output` 返回 `latest_message_id` 字段
- 当前 fallback：没有返回时 `action_id=None`

## 风险与局限

1. **backend 重启后断裂**：`_unprocessed` 是内存状态，重启后丢失
2. **多轮对话可能串**：一个 window 有多个 inbound 时，取最近一个
3. **消息丢失**：backend 处理中 crash，有 inbound 无 output

## 后续优化方向（暂不实现）

1. **持久化 _unprocessed 到 DB**：防止 backend 重启丢失
2. **时间窗口过滤**：避免多轮对话串到上一个 inbound
3. **确认机制完善**：已推送成功再通知 backend 确认

## 验收标准

1. DB 有 `action_id` 字段且能查询
2. inbound 和 outbound 的 action_id 一致时能关联
3. 查询链路正确：`SELECT * FROM messages WHERE action_id = ?`
# Action ID 链路追踪验证计划

## 目标

验证 `action_id` 字段能够正确关联一次 Q&A 的 inbound 和 outbound 消息。

## 验证步骤

### 1. DB Schema 验证

```bash
# 查看表结构
sqlite3 /path/to/gateway.db ".schema messages"

# 确认包含 action_id 字段
grep "action_id" /path/to/gateway.db
```

### 2. 单元测试验证

**测试文件:** `tests/test_gateway/test_db.py`

**新增测试类:** `TestActionId`

```python
class TestActionId:
    async def test_save_inbound_with_action_id(self, db):
        """验证 inbound 可以存储 action_id"""
        action_id = "test-action-001"
        await db.save_inbound({
            "id": "msg-001",
            "action_id": action_id,
            "timestamp": "2024-01-01T10:00:00",
            "backend_id": "test",
            "channel_type": "feishu",
            "chat_id": "chat1",
            "content": "test",
            "dedup_key": "dedup-001",
        })
        
        rows = await db.get_inbound_by_action_id(action_id)
        assert len(rows) == 1
        assert rows[0]["action_id"] == action_id

    async def test_save_outbound_with_action_id(self, db):
        """验证 outbound 可以存储 action_id"""
        action_id = "test-action-001"
        await db.save_outbound({
            "id": "out-001",
            "action_id": action_id,
            "timestamp": "2024-01-01T10:01:00",
            "backend_id": "test",
            "channel_type": "feishu",
            "chat_id": "chat1",
            "content": "test",
            "content_hash": "abc123",
        })
        
        # 通过 query_messages 查询
        rows, total = await db.query_messages({"action_id": action_id})
        assert total == 1
        assert rows[0]["direction"] == "outbound"

    async def test_query_by_action_id_links_inbound_outbound(self, db):
        """验证通过 action_id 能关联 inbound 和 outbound"""
        action_id = "q-001"
        
        # 保存 inbound
        await db.save_inbound({
            "id": "in-001",
            "action_id": action_id,
            "timestamp": "2024-01-01T10:00:00",
            "backend_id": "test",
            "channel_type": "feishu",
            "chat_id": "chat1",
            "content": "User question",
            "dedup_key": "dedup-001",
        })
        
        # 保存 outbound
        await db.save_outbound({
            "id": "out-001",
            "action_id": action_id,
            "timestamp": "2024-01-01T10:01:00",
            "backend_id": "test",
            "channel_type": "feishu",
            "chat_id": "chat1",
            "content": "Agent response",
            "content_hash": "hash1",
        })
        
        # 查询关联
        inbound_rows = await db.query_messages({"action_id": action_id, "direction": "inbound"})
        outbound_rows = await db.query_messages({"action_id": action_id, "direction": "outbound"})
        
        assert len(inbound_rows) == 1
        assert len(outbound_rows) == 1
        assert inbound_rows[0]["content"] == "User question"
        assert outbound_rows[0]["content"] == "Agent response"
```

### 3. 运行测试

```bash
# 运行 DB 相关测试
cd /home/irons/agentgate_team/agentgate
pytest tests/test_gateway/test_db.py::TestActionId -v

# 运行所有 DB 测试（确保没有破坏现有功能）
pytest tests/test_gateway/test_db.py -v
```

### 4. 集成测试验证

**场景:** 模拟完整 Q&A 链路

**步骤:**
1. 启动 backend（echo_backend 或 agentgate_backend）
2. 启动 gateway
3. 发送 inbound 消息（通过 HTTP 或直接调用）
4. 检查 DB 中 `messages` 表的 `action_id` 字段
5. 等待 backend 输出
6. 检查 outbound 的 `action_id` 是否相同
7. 查询：`SELECT * FROM messages WHERE action_id = 'xxx' ORDER BY timestamp`

**手动测试命令:**

```bash
# 查看 inbound
sqlite3 /home/irons/.agentgate/gateway/logs/gateway.db \
  "SELECT id, action_id, direction, content FROM messages WHERE direction='inbound' ORDER BY timestamp DESC LIMIT 5"

# 查看 outbound
sqlite3 /home/irons/.agentgate/gateway/logs/gateway.db \
  "SELECT id, action_id, direction, content FROM messages WHERE direction='outbound' ORDER BY timestamp DESC LIMIT 5"

# 关联查询
sqlite3 /home/irons/.agentgate/gateway/logs/gateway.db \
  "SELECT direction, content FROM messages WHERE action_id = 'your-action-id' ORDER BY timestamp"
```

### 5. 验证清单

- [ ] DB schema 包含 `action_id` 字段
- [ ] `save_inbound` 能存储 `action_id`
- [ ] `save_outbound` 能存储 `action_id`
- [ ] `get_inbound_by_action_id` 能查询
- [ ] `query_messages` 支持 `action_id` 过滤
- [ ] 单元测试通过
- [ ] 完整链路中 action_id 一致
- [ ] 手动查询能关联 inbound 和 outbound

### 6. 已知局限

1. **backend 重启后断裂**：`_unprocessed` 是内存状态，重启后丢失 latest_action_id
2. **多轮对话可能串**：一个 window 有多个 inbound 时，取最近一个
3. **消息丢失**：backend 处理中 crash，有 inbound 无 output

### 7. 后续优化（不在此次 PR 中）

1. 持久化 `_unprocessed` 到 DB
2. 时间窗口过滤避免多轮对话串接
3. 增强确认机制

## 验收标准

1. 所有单元测试通过
2. 手动验证能正确关联 Q&A 链路
3. 不破坏现有功能
