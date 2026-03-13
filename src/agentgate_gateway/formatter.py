import json
import re


def to_feishu_post(text: str) -> str:
    """Markdown -> Feishu post JSON string.

    Converts Markdown text to a Feishu post message structure that supports
    bold, code blocks, and inline code rendering.
    Returns a JSON string suitable for msg_type="interactive" card or "post".
    """
    content: list[list[dict]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Code block detection
        if line.strip().startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = "\n".join(code_lines)
            content.append([{"tag": "text", "text": code_text, "style": ["code_block"]}])
            continue

        # Regular line — parse inline elements
        if line.strip():
            elements = _parse_inline(line)
            content.append(elements)
        else:
            # Empty line as paragraph break
            content.append([{"tag": "text", "text": ""}])
        i += 1

    post = {"zh_cn": {"content": content}}
    return json.dumps(post, ensure_ascii=False)


def _parse_inline(line: str) -> list[dict]:
    """Parse inline Markdown (bold, inline code) into Feishu post elements."""
    elements: list[dict] = []
    # Pattern: **bold**, `code`, or plain text
    pattern = r'(\*\*(.+?)\*\*|`([^`]+)`)'
    last_end = 0

    for m in re.finditer(pattern, line):
        # Plain text before this match
        if m.start() > last_end:
            elements.append({"tag": "text", "text": line[last_end:m.start()]})

        if m.group(2):  # Bold
            elements.append({"tag": "text", "text": m.group(2), "style": ["bold"]})
        elif m.group(3):  # Inline code
            elements.append({"tag": "text", "text": m.group(3), "style": ["code_block"]})

        last_end = m.end()

    # Remaining plain text
    if last_end < len(line):
        elements.append({"tag": "text", "text": line[last_end:]})

    return elements or [{"tag": "text", "text": line}]


def to_feishu_rich(text: str) -> str:
    """Markdown -> Feishu post JSON (for backward compat, returns JSON string)."""
    return to_feishu_post(text)


def to_telegram_html(text: str) -> str:
    """Markdown -> Telegram HTML."""
    # Extract code blocks first to prevent inline processing inside them.
    # Replace with placeholders, restore after inline transforms.
    code_blocks: list[str] = []

    def stash_code_block(m: re.Match) -> str:
        code_blocks.append(f"<pre>{m.group(2)}</pre>")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r'```(\w*)\n(.*?)```', stash_code_block, text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic (single *)
    text = re.sub(
        r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)',
        r'<i>\1</i>',
        text,
    )

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text


def format_for_channel(channel_type: str, text: str) -> str:
    if channel_type == "feishu":
        return to_feishu_rich(text)
    elif channel_type == "telegram":
        return to_telegram_html(text)
    return text
