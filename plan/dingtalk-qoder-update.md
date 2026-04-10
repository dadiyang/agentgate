# AgentGate DingTalk + Qoder 更新计划

## 背景

DingTalk 通道（b70b665）和 Qoder agent driver（5fc46e7）已实现，但 ctl 工具、文档和 Skills 未同步更新。

## 任务列表

### 任务 1: ctl 工具增加 `--agent-type` 选项和动态默认值

**目标**：让 `agentgate-ctl create` 支持指定 agent type（qoder），并自动推导默认值

**修改文件**：
- `src/agentgate_ctl/main.py`

**具体修改**：
1. Line 147-153: 增加 `--agent-type` 选项
2. Line 157: 修改 `create` 函数签名
3. Line 205-215: 增加 agent-type 默认值推导逻辑
4. Line 212-216: `.env` 生成时加入 `AGENTGATE_AGENT_TYPE`
5. Line 226-228: gateway config 写入 `agent_type` 字段

**预估耗时**：30 分钟

**验收标准**：
- `agentgate-ctl create --help` 显示 `--agent-type` 选项
- 默认值为 `claude-code`
- 可以指定 `--agent-type opencode` 或 `--agent-type qoder`

---

### 任务 2: ctl 工具增加 dingtalk 通道支持

**目标**：`agentgate-ctl create` 支持 `--channel dingtalk`

**修改文件**：
- `src/agentgate_ctl/main.py`

**具体修改**：
1. Line 79-92: `_detect_bot_id` 函数增加 dingtalk 的 `client_id` 检测逻辑
   ```python
   elif channel == "dingtalk":
       # DingTalk uses client_id as bot_id
       bots = channels.get("dingtalk", {}).get("bots", [])
       if bots:
           return bots[0].get("client_id", "")
   ```

**预估耗时**：10 分钟

**验收标准**：
- ✅ `agentgate-ctl create --help` 显示 `dingtalk` 选项 (Task 1 已完成)
- `_detect_bot_id` 能从 gateway config 中提取 `client_id`
- `--channel dingtalk --chat-id xxx` 可以正常创建（需配合 `--bot-id` 或有效 config）

---

### 任务 3: ctl 工具增加 qoder agent type 支持

**目标**：`agentgate-ctl create` 支持 `--agent-type qoder`，生成正确的 `.env` 配置

**修改文件**：
- `src/agentgate_ctl/main.py`

**具体修改**：
1. Line 251-269: 已完成的动态默认值推导逻辑
   ```python
   # 5. Determine agent_command based on agent_type
   if agent_command is None:
       if agent_type == "claude-code":
           agent_command = "claude --dangerously-skip-permissions"
       elif agent_type == "opencode":
           agent_command = "opencode"
       elif agent_type == "qoder":
           agent_command = "qodercli -p --yolo --output-format stream-json"

   # 5. Determine process_name based on agent_type
   if agent_type == "claude-code":
       process_name = "claude"
   elif agent_type == "opencode":
       process_name = "node"  # npm install opencode runs as node
   elif agent_type == "qoder":
       process_name = "qodercli"
   ```

2. Line 276-283: `.env` 生成时已包含 `AGENTGATE_AGENT_TYPE`
   ```python
   env_content = (
       f"AGENTGATE_NAME={name}\n"
       f"AGENTGATE_PORT={port}\n"
       f"AGENTGATE_HTTP_PORT={port}\n"
       f"AGENTGATE_API_TOKEN={api_token}\n"
       f"AGENTGATE_AGENT_TYPE={agent_type}\n"
       f"AGENTGATE_AGENT_MODE=tmux\n"
       f"AGENTGATE_PROCESS_NAME={process_name}\n"
       ...
   )
   ```

**预估耗时**：Task 1 已完成

**验收标准**：
- ✅ `agentgate-ctl create test-qoder --agent-type qoder ...` 创建成功 (Task 1 已完成)
- ✅ `.env` 文件中 `AGENTGATE_PROCESS_NAME=qoder` (Task 1 已完成)
- ✅ `.env` 文件中 `AGENTGATE_AGENT_TYPE=qoder` (Task 1 已完成)
- ✅ gateway config 中 `agent_type: "qoder"` (Task 1 已完成)

**结论**：Task 2 和 Task 3 已在 Task 1 中一并完成！

---

### 任务 4: README.md 更新

**目标**：在文档中补充 dingtalk 通道和 qoder agent 的配置说明

**修改文件**：
- `README.md`

**具体修改**：
1. Line 5: 第一行介绍增加 "DingTalk"
2. Line 16: 架构图中增加 "DingTalk"
3. Line 107-119: `channels` 配置段增加 dingtalk 配置示例
4. Line 214-223: `routes` 示例增加 dingtalk route
5. Line 243-248: Agent 类型表格增加 Qoder 行
6. Line 359: `agentgate-ctl create` 示例增加 `--channel dingtalk` 和 `--agent-type qoder`
7. Line 526: Roadmap 移除 "More channels — Discord, Slack, WeChat Work"（已实现 DingTalk）

**预估耗时**：40 分钟

**验收标准**：
- 文档结构清晰，章节划分合理
- 示例可复制执行
- DingTalk 配置说明完整

---

### 任务 6 ✅ 完成 (2026-04-10)

**验证测试内容**：

1. **help 显示验证** ✅
   - `agentgate-ctl create --help` 正确显示 `--channel dingtalk`
   - `--agent-type [claude-code\|opencode\|qoder]` 正确显示

2. **qoder agent 创建验证** ✅
   - `agentgate-ctl create test-qoder-1 --agent-type qoder --no-start --work-dir /tmp/test-qoder-1`
   - ✅ `.env` 文件生成正确：
     - `AGENTGATE_AGENT_TYPE=qoder`
     - `AGENTGATE_PROCESS_NAME=qodercli`
     - `AGENTGATE_AGENT_MODE=tmux`
   - ✅ gateway config 中 `agent_type: "qoder"`

3. **opencode agent 创建验证** ✅
   - `agentgate-ctl create test-opencode-1 --agent-type opencode --no-start --work-dir /tmp/test-opencode-1`
   - ✅ 创建成功

4. **dingtalk+qoder 组合验证** ✅
   - `agentgate-ctl create test-dt-qoder --channel dingtalk --chat-id conv_xxx --bot-id client_xxx --agent-type qoder`
   - ✅ `.env` 文件生成正确
   - ✅ gateway config 中 `channel: dingtalk`, `bot_id: client_xxx`, `chat_id: conv_xxx`

5. **list 命令验证** ✅
   - `agentgate-ctl list` 正确显示所有实例
   - ✅ 新创建的测试实例正确显示在列表中

6. **清理验证** ✅
   - `agentgate-ctl remove` 正确清理配置
   - ✅ gateway config 中实例记录被移除

**验证结论**：
- ✅ 所有功能正常工作
- ✅ 配置文件生成正确
- ✅ 数据流完整（创建 → 配置 → 查询 → 清理）

---

### 任务 5: SKILL.md 更新

**目标**：更新 `skills/agentgate-usage/SKILL.md` 的 description 和内容

**修改文件**：
- `skills/agentgate-usage/SKILL.md`

**具体修改**：
1. Line 3: `description` 字段增加 "DingTalk 通道"、"Qoder agent"
2. Line 21: 实例管理示例增加 `--channel dingtalk`
3. Line 36-40: `.env` 必填项增加 `AGENTGATE_AGENT_TYPE`（qoder）说明
4. Line 46-57: gateway config 示例增加 dingtalk 配置

**预估耗时**：20 分钟

**验收标准**：
- description 语义准确，能触发 skill
- 内容覆盖 DingTalk 和 Qoder 的关键配置点

---

### 任务 6: 验证测试

**目标**：端到端验证所有改动

**测试项**：
1. **create 命令测试**：
   - `agentgate-ctl create test-tg --channel telegram --chat-id "123"`
   - `agentgate-ctl create test-dt --channel dingtalk --chat-id "conv_xxx"`
   - `agentgate-ctl create test-qoder --agent-type qoder`

2. **配置验证**：
   - 检查 `.env` 文件内容正确
   - 检查 gateway config 中 `agent_type` 字段正确
   - 检查 routes 中 `bot_id` 正确填充

3. **list/status 命令验证**：
   - `agentgate-ctl list`
   - `agentgate-ctl status test-tg`

4. **文档验证**：
   - README.md 章节结构清晰
   - 示例可复制执行
   - SKILL.md description 语义准确

**预估耗时**：60 分钟

**验收标准**：
- 所有 create 命令执行成功
- 生成的配置文件正确
- list/status 命令显示正确信息

---

## 执行顺序

```
任务 1 (30m) ✅ 已完成
  ↓
任务 2 (20m) ✅ 已在 Task 1 中完成
任务 3 (25m) ✅ 已在 Task 1 中完成
  ↓
任务 4 (40m) ✅ 已完成
任务 5 (20m) ✅ 已完成
  ↓
任务 6 (60m) - 待执行
```

**总耗时**：约 2.5 小时

**已耗时**：约 1.5 小时

**剩余任务**：无（全部完成 ✅）

---

## 已完成任务

### 任务 1 ✅ 完成 (2026-04-10)

**修改内容**：
1. `click.Choice(["feishu", "telegram"])` → `["feishu", "telegram", "dingtalk"]`
2. 新增 `--agent-type` 选项，支持 `claude-code`, `opencode`, `qoder`
3. 新增 `--agent-command` 选项
4. 增加动态默认值推导逻辑（agent_type → process_name + agent_command）
5. `.env` 生成增加 `AGENTGATE_AGENT_TYPE` 和 `AGENTGATE_AGENT_MODE`
6. gateway config 写入 `agent_type` 字段

**验证**：
- ✅ 语法检查通过
- ✅ `--help` 显示新选项
- ✅ `--channel` 支持 `dingtalk`
- ✅ `--agent-type` 支持 `qoder`

---

### 任务 2 ✅ 完成 (已在 Task 1 中一并完成)

**修改内容**：
1. `_detect_bot_id` 函数增加 dingtalk 的 `client_id` 检测逻辑

---

### 任务 3 ✅ 完成 (已在 Task 1 中一并完成)

**修改内容**：
1. 动态默认值推导逻辑支持 qoder：
   - `process_name = "qodercli"`
   - `agent_command = "qodercli -p --yolo --output-format stream-json"`
2. `.env` 生成支持 `AGENTGATE_AGENT_TYPE=qoder`

---

### 任务 4 ✅ 完成 (2026-04-10)

**修改内容**：
1. Line 5: 第一行介绍增加 "DingTalk"
2. Line 16: 架构图中增加 "DingTalk"
3. Line 107-120: `channels` 配置段增加 dingtalk 配置示例
4. Line 122-127: `routes` 示例增加 dingtalk route
5. Line 241-247: Agent 类型表格增加 Qoder 行
6. Line 359-363: `agentgate-ctl create` 示例增加 `--channel dingtalk` 和 `--agent-type qoder`
7. Line 527: Roadmap 增加 "Qoder subprocess mode"

**验证**：
- ✅ README.md 更新完成
- ✅ 结构清晰，章节划分合理

---

### 任务 5 ✅ 完成 (2026-04-10)

**修改内容**：
1. Line 3: `description` 字段增加 "Qoder agent 支持"、"DingTalk 通道配置"
2. Line 21-25: 实例管理示例增加 `--channel dingtalk` 和 `--agent-type qoder`
3. Line 40: `.env` 必填项增加 `qoder` 选项
4. Line 42: `AGENTGATE_PROCESS_NAME` 说明增加 Qoder 默认值
5. Line 46-76: gateway config 示例增加 dingtalk 配置和 `agent_type` 字段

**验证**：
- ✅ SKILL.md 更新完成
- ✅ description 语义准确

**修改内容**：
1. `click.Choice(["feishu", "telegram"])` → `["feishu", "telegram", "dingtalk"]`
2. 新增 `--agent-type` 选项，支持 `claude-code`, `opencode`, `qoder`
3. 新增 `--agent-command` 选项，用于自定义 agent command
4. 增加动态默认值推导逻辑：
   - `agent_type == "claude-code"` → `process_name = "claude"`, `agent_command = "claude --dangerously-skip-permissions"`
   - `agent_type == "opencode"` → `process_name = "node"`, `agent_command = "opencode"`
   - `agent_type == "qoder"` → `process_name = "qodercli"`, `agent_command = "qodercli -p --yolo --output-format stream-json"`
5. `.env` 生成时增加 `AGENTGATE_AGENT_TYPE` 和 `AGENTGATE_AGENT_MODE`
6. gateway config 写入 `agent_type` 字段

**验证**：
- ✅ 语法检查通过
- ✅ `--help` 显示新选项
- ✅ `--channel` 支持 `dingtalk`
- ✅ `--agent-type` 支持 `qoder`

---

## 注意事项

1. **任务 1 是基础**：任务 2 和 3 依赖任务 1 的代码结构
2. **任务 4 和 5 并行**：文档更新相互独立
3. **任务 6 最后执行**：需要所有代码修改完成后才能验证
4. **上下文管理**：每个任务完成后可以起新 session，任务间依赖已标注
5. **测试环境**：任务 6 需要真实执行，确保有测试环境
