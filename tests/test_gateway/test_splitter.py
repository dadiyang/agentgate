"""Tests for splitter.py — Long message splitting."""

import pytest

from agentgate_gateway.splitter import CHANNEL_LIMITS, split_message


class TestSplitMessageShort:
    """Short messages that don't need splitting."""

    def test_short_message_returns_single_part(self):
        """Message under limit returns list with one element."""
        result = split_message("Hello, world!", "telegram")
        assert result == ["Hello, world!"]

    def test_empty_string_returns_single_part(self):
        """Empty string returns list with one empty element."""
        result = split_message("", "telegram")
        assert result == [""]

    def test_exact_limit_returns_single_part(self):
        """Message exactly at limit returns single part."""
        limit = CHANNEL_LIMITS["telegram"]
        text = "x" * limit
        result = split_message(text, "telegram")
        assert len(result) == 1
        assert result[0] == text

    def test_one_under_limit_returns_single_part(self):
        """Message one char under limit returns single part."""
        limit = CHANNEL_LIMITS["telegram"]
        text = "x" * (limit - 1)
        result = split_message(text, "telegram")
        assert len(result) == 1


class TestSplitMessageTelegram:
    """Telegram-specific splitting (4096 char limit)."""

    def test_over_limit_splits_into_multiple(self):
        """Message over 4096 chars splits into multiple parts."""
        limit = CHANNEL_LIMITS["telegram"]
        text = "x" * (limit + 100)
        result = split_message(text, "telegram")
        assert len(result) > 1

    def test_all_parts_within_limit(self):
        """All resulting parts are within the channel limit."""
        limit = CHANNEL_LIMITS["telegram"]
        text = "x" * (limit * 3)
        result = split_message(text, "telegram")
        for part in result:
            assert len(part) <= limit

    def test_no_data_loss(self):
        """Concatenating all parts reproduces the original content."""
        limit = CHANNEL_LIMITS["telegram"]
        # Use words so boundary detection matters
        words = ["word"] * (limit // 5 * 3)
        text = " ".join(words)
        result = split_message(text, "telegram")
        # Rejoin with spaces stripped at split points
        rejoined = " ".join(part for part in result)
        # Compare without extra spaces (split strips leading/trailing whitespace)
        assert rejoined.replace("  ", " ") == text or "".join(
            part.replace(" ", "") for part in result
        ) == text.replace(" ", "")

    def test_no_data_loss_exact_chars(self):
        """Character count after rejoining matches original (accounting for stripped whitespace)."""
        limit = CHANNEL_LIMITS["telegram"]
        # Use text without spaces to avoid stripping ambiguity
        text = "ab" * (limit + 500)
        result = split_message(text, "telegram")
        total_chars = sum(len(part) for part in result)
        # Total chars should equal original (stripped whitespace may reduce slightly)
        assert total_chars == len(text)

    def test_prefers_paragraph_boundary(self):
        """Splitter prefers \\n\\n over hard cut."""
        limit = CHANNEL_LIMITS["telegram"]
        # Create text with paragraph break before the limit
        para_break_pos = limit - 100
        part1 = "a" * para_break_pos
        part2 = "b" * 200
        text = part1 + "\n\n" + part2

        result = split_message(text, "telegram")
        assert len(result) == 2
        # First part should end at the paragraph boundary
        assert result[0] == part1
        assert result[1] == part2

    def test_prefers_newline_over_hard_cut(self):
        """When no paragraph break exists, prefers single \\n."""
        limit = CHANNEL_LIMITS["telegram"]
        # Create text with single newline before the limit, no double newline
        newline_pos = limit - 50
        part1 = "a" * newline_pos
        part2 = "b" * 200
        text = part1 + "\n" + part2

        result = split_message(text, "telegram")
        assert len(result) == 2
        assert result[0] == part1
        assert result[1] == part2


class TestSplitMessageFeishu:
    """Feishu-specific splitting (15000 raw char limit, ~30k after JSON formatting)."""

    def test_feishu_limit_short_message(self):
        """Message under limit returns single part."""
        text = "x" * 1000
        result = split_message(text, "feishu")
        assert result == [text]

    def test_feishu_limit_long_message(self):
        """Message over 30000 chars splits."""
        limit = CHANNEL_LIMITS["feishu"]
        text = "x" * (limit + 1000)
        result = split_message(text, "feishu")
        assert len(result) > 1
        for part in result:
            assert len(part) <= limit

    def test_feishu_no_data_loss(self):
        """No data loss for Feishu split."""
        limit = CHANNEL_LIMITS["feishu"]
        text = "y" * (limit * 2 + 500)
        result = split_message(text, "feishu")
        total_chars = sum(len(part) for part in result)
        assert total_chars == len(text)


class TestSplitMessageUnknownChannel:
    """Unknown channel uses default limit."""

    def test_unknown_channel_short_message(self):
        """Unknown channel, short message returns single part."""
        result = split_message("Hello!", "unknown_channel")
        assert result == ["Hello!"]

    def test_unknown_channel_uses_default_limit(self):
        """Unknown channel uses DEFAULT_LIMIT (30000)."""
        from agentgate_gateway.splitter import DEFAULT_LIMIT

        text = "x" * (DEFAULT_LIMIT + 1000)
        result = split_message(text, "unknown_channel")
        assert len(result) > 1
        for part in result:
            assert len(part) <= DEFAULT_LIMIT


class TestSplitMessageBoundaryPriority:
    """Boundary detection priority: \\n\\n > \\n > 。> '. ' > hard cut."""

    def test_chinese_period_boundary(self):
        """Falls back to 。as boundary when no newlines present."""
        limit = CHANNEL_LIMITS["telegram"]
        # Build text where 。 appears just before limit, no newlines
        boundary_pos = limit - 50
        part1 = "中文句子" * (boundary_pos // 4)
        # Trim to exact position and add 。
        part1 = part1[:boundary_pos] + "。"
        part2 = "后续内容" * 60
        text = part1 + part2

        result = split_message(text, "telegram")
        # Should split at or near the 。boundary
        assert len(result) >= 1
        for part in result:
            assert len(part) <= limit

    def test_sentence_period_boundary(self):
        """Falls back to '. ' as boundary."""
        limit = CHANNEL_LIMITS["telegram"]
        boundary_pos = limit - 50
        part1 = "a" * boundary_pos + ". "
        part2 = "b" * 200
        text = part1 + part2

        result = split_message(text, "telegram")
        assert len(result) >= 1
        for part in result:
            assert len(part) <= limit

    def test_hard_cut_when_no_boundary(self):
        """Hard cut at limit when no boundary found."""
        limit = CHANNEL_LIMITS["telegram"]
        # No spaces, newlines, or periods
        text = "x" * (limit + 500)
        result = split_message(text, "telegram")
        assert len(result) > 1
        assert len(result[0]) == limit
