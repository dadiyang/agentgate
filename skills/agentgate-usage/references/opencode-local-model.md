# OpenCode + 本地模型 (llama-server) 配置

## 1. 启动 llama-server

```bash
llama-server -m Qwen3.5-35B-A3B.gguf --port 18090 --ctx-size 262144 --parallel 2
```

## 2. 全局 OpenCode provider

在 `~/.config/opencode/opencode.json` 的 `provider` 字段加：

```json
"local": {
  "npm": "@ai-sdk/openai-compatible",
  "name": "Local llama-server",
  "options": {
    "baseURL": "http://127.0.0.1:18090/v1",
    "apiKey": "dummy"
  },
  "models": {
    "Qwen3.5-35B": {
      "name": "Qwen3.5-35B",
      "attachments": false,
      "limit": {
        "context": 131072,
        "output": 8192
      }
    }
  }
}
```

**`limit.context` = llama-server 的 `ctx-size / parallel`**。设小了会触发 auto-compaction 死循环。

## 3. Backend .env

```bash
AGENTGATE_AGENT_TYPE=opencode
AGENTGATE_AGENT_MODE=tmux
AGENTGATE_OPENCODE_MODEL=local/Qwen3.5-35B
# AGENTGATE_PROCESS_NAME 默认 "node"（npm 安装）
# 原生编译的 OC 二进制需设为 "opencode"
# 验证：tmux list-windows -F '#{pane_current_command}'
```

## 4. 项目级模型覆盖

工作目录放 `opencode.json`：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "local/Qwen3.5-35B"
}
```

不同 backend 用不同模型，共享全局 provider。

## 5. 自动权限配置

AgentGate 启动 OC backend 时自动 patch 工作目录的 `opencode.json`：
- 所有工具 = allow（无人值守运行）
- question = deny（AskUser 弹窗无法通过 IM 回答）

## 常见问题

| 问题 | 原因 | 修复 |
|------|------|------|
| Auto-compaction 死循环 | `limit.context` 配太小 | 设为 ctx-size/parallel |
| SelfMonitor 反复重启 OC | `PROCESS_NAME` 与 tmux 前台进程名不匹配 | `tmux list-windows -F '#{pane_current_command}'` 查看实际值，npm 安装默认 `node`，原生二进制用 `opencode` |
| OC AskUser 弹窗卡住 | 全局 opencode.json 没有 question=deny | AgentGate 自动处理，重启 backend |
