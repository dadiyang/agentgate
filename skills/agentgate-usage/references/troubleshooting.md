# AgentGate 接入排查详解

## "Session file no longer exists for window_id @389"

CC tmux 模式下，session_map.json 记录窗口 ID → session_id 映射。tmux server 重启后窗口 ID 变了（@389 → @401），但 state.json 还存着旧 ID。

```bash
# 看当前窗口 ID
tmux list-windows -t agentgate-my-project-dev

# 看 state.json 记录的
cat ~/.agentgate/backends/my-project-dev/state.json | python3 -m json.tool

# 看 session_map.json
cat ~/.agentgate/backends/my-project-dev/session_map.json | python3 -m json.tool
```

修复：重启 backend（启动时自动 re-resolve stale IDs）。

## Output poller 持续 404

gateway config 的 `default_window` 与实际 tmux 窗口名不匹配。

```bash
# 实际窗口名
tmux list-windows -t agentgate-my-project-dev -F "#{window_name}"

# config 中的
grep -A3 "my-project-dev" ~/.agentgate/gateway/config.yaml
```

修复：改 config → 热加载。

## "No route matched"

route 的 `(channel, bot_id, chat_id)` 三元组与实际消息不一致。

```bash
journalctl -u agentgate-gateway --since "5 min ago" | grep "No route matched"
```

日志打出实际的 channel/bot_id/chat_id，对比 config。常见错误：
- Telegram bot_id 没加引号（YAML 解析为数字，类型不匹配）
- 飞书 chat_id 用了群名称而非 `oc_xxxx` ID
- 字段名写成 `backend_id` 而非 `backend`

## 端口冲突

`Address already in use` → `ss -tlnp | grep 8950`

## tmux session 不存在

`session not found` → backend 会创建窗口但不创建 session。

```bash
tmux has-session -t agentgate-my-project-dev 2>&1
# 不存在则创建
tmux new-session -d -s agentgate-my-project-dev -n __main__
```

## CC OAuth 过期

Agent 窗口卡在 `Paste code here` 或 `oauth/authorize`。SelfMonitor 会标记为 `degraded`。

修复：任意终端 `claude /login`（OAuth 全局生效）。

## Subprocess session_id 丢失

重启后 agent 没上下文。检查：`cat ~/.agentgate/backends/my-project-dev/cc_subprocess_session_id`

文件不存在或为空 = 进程被强杀时没保存。不可恢复，agent 以新 session 启动。

## 飞书 / Telegram adapter 启动失败

- 飞书：检查 app_id / app_secret，确认能直连飞书 API（不走代理）
- Telegram：检查 bot_token，确认代理配置（config.yaml 的 adapter 级 `proxy` 字段）

## CC hook 和 session_map.json 路径

CC 的 UserPromptSubmit hook 写 session_map.json 到 `~/.agentgate/backends/<name>/session_map.json`。路径由 `AGENTGATE_DIR` 或 `data_dir / name` 决定。如果自定义了 `AGENTGATE_DATA_DIR`，hook 写入路径和 backend 读取路径必须一致。
