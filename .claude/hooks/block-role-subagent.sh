#!/bin/bash
# PreToolUse hook: 禁止用 Agent 工具的 subagent 替代独立角色。
# 只拦截 Agent 工具调用，检查 subagent_type 是否匹配被禁列表。
# Exit 0 = allow, Exit 2 = block.
#
# 配合 blocked-subagents.conf 使用（每行一个被禁的 subagent_type 模式）。

CONF_FILE="$(dirname "$0")/blocked-subagents.conf"
if [[ ! -f "$CONF_FILE" ]]; then
    exit 0
fi

input=$(cat)

# Only intercept Agent tool
tool_name=$(echo "$input" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null)
if [[ "$tool_name" != "Agent" ]]; then
    exit 0
fi

# Extract subagent_type from tool_input
subagent_type=$(echo "$input" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('tool_input',{}).get('subagent_type',''))
" 2>/dev/null)

# Read blocked patterns
mapfile -t BLOCKED < <(grep -v '^#' "$CONF_FILE" | grep -v '^$')

if [[ ${#BLOCKED[@]} -eq 0 ]]; then
    exit 0
fi

for pattern in "${BLOCKED[@]}"; do
    if [[ "$subagent_type" == "$pattern" ]]; then
        echo "BLOCKED: 禁止用 subagent 替代 '$pattern' 角色——该角色有独立 ccbot 实例在运行。"
        echo "正确做法：send-to <角色名> \"你的指令\"。例如：send-to dev \"请实现 F1 功能\""
        echo "用 send-to --list 查看所有可用角色。"
        exit 2
    fi
done

exit 0
