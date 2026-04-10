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
