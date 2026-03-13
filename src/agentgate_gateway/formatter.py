import re


def to_feishu_rich(text: str) -> str:
    """Markdown -> Feishu text (Feishu natively supports some markdown)."""
    return text  # Feishu text messages render markdown partially


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
