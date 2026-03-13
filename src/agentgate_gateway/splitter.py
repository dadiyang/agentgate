# Raw text limits BEFORE channel-specific formatting.
# Feishu post JSON wraps each line in {"tag":"text","text":"..."} — roughly 2x
# expansion, so raw text limit is half the API's ~30000 char content limit.
CHANNEL_LIMITS = {"feishu": 15000, "telegram": 4096}
DEFAULT_LIMIT = 30000


def split_message(text: str, channel_type: str) -> list[str]:
    limit = CHANNEL_LIMITS.get(channel_type, DEFAULT_LIMIT)
    if len(text) <= limit:
        return [text]

    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Prefer paragraph boundary
        cut = text.rfind('\n\n', 0, limit)
        if cut == -1:
            cut = text.rfind('\n', 0, limit)
        if cut == -1:
            cut = text.rfind('。', 0, limit)
        if cut == -1:
            cut = text.rfind('. ', 0, limit)
        if cut == -1:
            cut = limit
        else:
            cut += 1
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts
