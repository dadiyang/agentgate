# agentgate 工程教训 — 2026-03-21

## [2026-03-21] [agentgate] — SQLite 统一消息表（单表取代双表）

**发现**：inbound_messages + outbound_messages 双表在跨方向查询、恢复逻辑、API 聚合时代码重复严重。

**解法**：`messages` 单表加 `direction TEXT NOT NULL`（inbound/outbound），旧双表废弃：

```python
# db.py 新方法签名
save_inbound(msg_id, backend_id, channel_type, bot_id, chat_id, group_name,
             sender_id, sender_name, content, dedup_key) -> None
save_outbound(msg_id, backend_id, channel_type, bot_id, chat_id,
              content, shard_index, shard_total, content_hash) -> None
update_status(msg_id, status, error_message=None) -> None
increment_retry(msg_id) -> None
get_pending(direction) -> list[dict]           # direction = "inbound" | "outbound"
get_failed(direction, backend_id=None) -> list[dict]
has_dedup_key(dedup_key) -> bool
has_content_hash(backend_id, content_hash) -> bool
```

**调用方改动摘要**：
- `inbound_handler.py`：`update_inbound_delivery` → `update_status`，`increment_inbound_retry` → `increment_retry`
- `output_poller.py`：`update_outbound_push` → `update_status`，`has_outbound_content_hash` → `has_content_hash`，`"pushed"` 状态统一改为 `"delivered"`
- `recovery.py`：`get_pending_inbound/outbound` → `get_pending("inbound/outbound")`，`get_failed_*` → `get_failed("inbound/outbound", backend_id)`
- `api.py`：同上 get_pending 改法

**教训**：DB 统一之前确认所有调用方，全局 grep 旧方法名。status 字段只用三个值 pending/delivered/failed，去掉 pushed/processed 等中间态——中间态能用日志追踪，DB 不用存。

---

## [2026-03-21] [agentgate] — CC stream-json subprocess 中途纠偏：协议级不可能

**结论**：CC subprocess（stream-json）模式下，无法在 agent 执行过程中注入纠偏指令。**这是协议约束，不是实现限制。**

**验证过程**：
1. 实测 subprocess 中途发送纠偏消息 → CC 把纠偏内容当工具结果/prompt 注入，原任务按原计划完成，纠偏被忽略
2. cc-connect `engine.go` 源码注释（lines 1137-1141）明确写道：
   ```
   // Only queue metadata — do NOT send to agent stdin yet.
   // The agent CLI may treat a mid-turn stdin message as part of the
   // current turn, causing the event loop to hang waiting for a second
   // EventResult that never arrives.
   ```
3. CC stream-json 协议控制消息类型只有三种：`control_request`（CC→client）、`control_response`（client→CC）、`control_cancel_request`（CC→client）——无 client 发起的 cancel/abort
4. SIGINT 测试：向 CC `-p` 进程发 SIGINT → 进程直接终止（`error_during_execution`），不做优雅中断

**tmux 为何可行**：tmux send-keys 写入终端缓冲区，CC 完成一次工具调用后**主动检查键盘输入**，此时看到纠偏消息并处理。这是 CC TUI 模式的设计，不适用于 stream-json 协议模式。

**结论对 README/产品定位的影响**：subprocess 模式下"消息被排队等当前轮次结束"的描述是正确的，但应更明确说明这是协议设计而非队列实现的问题。

---

## [2026-03-21] [agentgate] — Context compression 消息过滤

**场景**：CC 上下文压缩时输出一条极长消息，以 `"👤 This session is being continued from a previous conversation"` 开头，推到 IM 会刷屏且无意义。

**解法**：在 `output_poller._push_to_channel` 中，`save_outbound`（DB 保留完整内容）之后、`_push_with_retry` 之前，检测并替换推送文本：

```python
_CONTEXT_SUMMARY_PREFIX = "This session is being continued from a previous conversation"
_CONTEXT_SUMMARY_NOTICE = "[上下文已压缩，对话继续]"

push_text = part
stripped = part.lstrip("\U0001f464 ")   # 去掉前置 👤 和空格
if stripped.startswith(_CONTEXT_SUMMARY_PREFIX):
    push_text = _CONTEXT_SUMMARY_NOTICE
    logger.info("Context summary replaced for IM push (msg_id=%s, original_len=%d)", msg_id, len(part))
await self._push_with_retry(adapter, msg_id, chat_id, push_text)
```

**关键设计**：`save_outbound` 在替换前执行，DB 里存的是原始全文，便于事后排查。IM 只推简短通知。

**注意**：`lstrip("\U0001f464 ")` 去掉 `👤 ` 前缀才能正确匹配英文开头，否则 startswith 永远 False。

---

## [2026-03-21] [agentgate] — README 写作经验

**场景**：写开源项目 README，综合 PM 产品定位文档 + 技术实现细节。

**有效结构**（以 agentgate 为例）：
1. 一句话 tagline（产品类型 + 核心价值）
2. 问题叙事（从用户真实痛点切入，不从功能切入）
3. 核心差异化特性（最重要的放第一位，tmux 中途纠偏）
4. 路由配置 + 热加载（具体 YAML 示例）
5. 混用 Agent / 成本控制（表格对比）
6. tmux vs subprocess 对比表
7. 高可用分层表
8. 架构图（ASCII）
9. 快速开始（前置条件 → 安装 → 配置 → 启动）
10. API 参考
11. 路线图
12. 致谢

**PM 文档转化规则**：
- PM 的"场景"→ README 的问题叙事段落
- PM 的"核心价值主张"→ tagline + 功能小节
- PM 的"为什么 tmux"→ mid-task correction 小节
- PM 的技术风险/PM备注 → 不进 README

**陷阱**：技术写手容易假设"读者用 tmux"，实际上第一个痛点场景应从"你在午饭时"等移动场景切入，让不懂 tmux 的人也能理解价值。
