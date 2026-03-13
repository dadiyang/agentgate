"""Tests for formatter.py — Markdown to channel format conversion."""

import pytest

from agentgate_gateway.formatter import (
    format_for_channel,
    to_feishu_rich,
    to_telegram_html,
)


class TestToTelegramHtml:
    """Test Markdown -> Telegram HTML conversion."""

    def test_bold_conversion(self):
        """**text** -> <b>text</b>"""
        result = to_telegram_html("**hello world**")
        assert result == "<b>hello world</b>"

    def test_bold_inline(self):
        """Bold within sentence."""
        result = to_telegram_html("This is **important** text")
        assert result == "This is <b>important</b> text"

    def test_code_block(self):
        """```...``` -> <pre>...</pre>"""
        result = to_telegram_html("```python\nprint('hello')\n```")
        assert "<pre>" in result
        assert "</pre>" in result
        assert "print('hello')" in result

    def test_code_block_no_language(self):
        """Code block without language tag."""
        result = to_telegram_html("```\nsome code\n```")
        assert "<pre>" in result
        assert "some code" in result

    def test_inline_code(self):
        """`x` -> <code>x</code>"""
        result = to_telegram_html("`variable`")
        assert result == "<code>variable</code>"

    def test_inline_code_in_sentence(self):
        """Inline code within sentence."""
        result = to_telegram_html("Use `git status` to check")
        assert result == "Use <code>git status</code> to check"

    def test_italic_single_asterisk(self):
        """*text* -> <i>text</i>"""
        result = to_telegram_html("*italic text*")
        assert result == "<i>italic text</i>"

    def test_italic_not_bold(self):
        """Single * is italic, double ** is bold — they don't conflict."""
        result = to_telegram_html("**bold** and *italic*")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result

    def test_code_block_before_inline_processing(self):
        """Code block content should not be processed for bold/italic."""
        result = to_telegram_html("```\n**not bold**\n```")
        # The ** inside code block should be preserved, not converted to <b>
        assert "<pre>" in result
        # Bold tags should NOT appear inside pre
        assert "<b>" not in result

    def test_multiple_bold(self):
        """Multiple bold segments in same text."""
        result = to_telegram_html("**a** and **b**")
        assert result == "<b>a</b> and <b>b</b>"

    def test_multiple_inline_code(self):
        """Multiple inline code segments."""
        result = to_telegram_html("`a` and `b`")
        assert result == "<code>a</code> and <code>b</code>"

    def test_plain_text_unchanged(self):
        """Plain text without markdown passes through."""
        result = to_telegram_html("Hello, world!")
        assert result == "Hello, world!"

    def test_empty_string(self):
        """Empty string returns empty string."""
        result = to_telegram_html("")
        assert result == ""


class TestToFeishuRich:
    """Test Feishu passthrough (Feishu renders markdown natively)."""

    def test_feishu_passes_through_unchanged(self):
        """Feishu formatter returns text unchanged."""
        text = "**bold** and `code` and *italic*"
        result = to_feishu_rich(text)
        assert result == text

    def test_feishu_plain_text(self):
        """Plain text passes through."""
        result = to_feishu_rich("Hello Feishu!")
        assert result == "Hello Feishu!"

    def test_feishu_empty(self):
        """Empty string passes through."""
        result = to_feishu_rich("")
        assert result == ""


class TestFormatForChannel:
    """Test format_for_channel dispatch."""

    def test_dispatches_to_telegram(self):
        """format_for_channel('telegram', ...) applies Telegram conversion."""
        result = format_for_channel("telegram", "**bold**")
        assert result == "<b>bold</b>"

    def test_dispatches_to_feishu(self):
        """format_for_channel('feishu', ...) passes through."""
        text = "**bold**"
        result = format_for_channel("feishu", text)
        assert result == text

    def test_unknown_channel_returns_plain(self):
        """Unknown channel type returns text unchanged."""
        text = "**some text**"
        result = format_for_channel("discord", text)
        assert result == text

    def test_unknown_channel_empty_string(self):
        """Unknown channel type with empty string."""
        result = format_for_channel("slack", "")
        assert result == ""

    def test_telegram_inline_code_dispatch(self):
        """`code` via format_for_channel for telegram."""
        result = format_for_channel("telegram", "`myvar`")
        assert result == "<code>myvar</code>"
