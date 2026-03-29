"""Reusable catch-up logic for event replay after agent recovery.

Pure functions, zero external dependencies. Any ccbot consumer can import
this module to classify undelivered events and generate catch-up summaries.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CatchupPolicy:
    """Thresholds for classifying event staleness."""
    fresh_threshold_s: float = 300.0   # 5 min
    max_age_s: float = 14400.0         # 4 hours


def classify_events(
    events: list[dict],
    policy: CatchupPolicy,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Classify events into (fresh, stale, expired) based on age_s.

    Args:
        events: List of dicts, each must have 'age_s' key (float, seconds).
        policy: Thresholds for classification.

    Returns:
        (fresh, stale, expired) -- three lists of the same event dicts.
        fresh: age < fresh_threshold_s (replay individually)
        stale: fresh_threshold_s <= age < max_age_s (aggregate into summary)
        expired: age >= max_age_s (skip, log warning)
    """
    fresh, stale, expired = [], [], []
    for evt in events:
        age = evt["age_s"]
        if age < policy.fresh_threshold_s:
            fresh.append(evt)
        elif age < policy.max_age_s:
            stale.append(evt)
        else:
            expired.append(evt)
    return fresh, stale, expired


# ---------------------------------------------------------------------------
# Catch-up summary generation
# ---------------------------------------------------------------------------

_LIST_ITEM_LIMIT = 10


def _extract_hhmm(wall_time: str) -> str:
    """Extract HH:MM from an ISO-8601 datetime string."""
    try:
        t_idx = wall_time.index("T")
        return wall_time[t_idx + 1 : t_idx + 6]  # "HH:MM"
    except (ValueError, IndexError) as e:
        logger.debug("_extract_hhmm: could not parse time from %r: %s", wall_time, e)
        return "??:??"


def generate_catchup_summary(
    stale_events: list[dict],
    tag_config: dict[str, str],
) -> str:
    """Generate a human-readable catch-up summary for stale events.

    Args:
        stale_events: List of event dicts with keys: tag, text_hint, wall_time.
        tag_config: {tag_name: "list"|"count"}. Tags not present default to "count".
                    Events with tag=None are counted and displayed as "其他".

    Returns:
        Multi-line summary string, or "" if stale_events is empty.
    """
    if not stale_events:
        return ""

    total = len(stale_events)

    # Sort by wall_time for display order
    sorted_events = sorted(stale_events, key=lambda e: e.get("wall_time", ""))

    # Separate into list-items and count-items
    list_items: list[dict] = []
    count_buckets: dict[str, int] = defaultdict(int)

    for evt in sorted_events:
        tag = evt.get("tag")
        mode = tag_config.get(tag, "count") if tag is not None else "count"
        if mode == "list":
            list_items.append(evt)
        else:
            display_tag = tag if tag is not None else "其他"
            count_buckets[display_tag] += 1

    # Build output
    lines: list[str] = []
    lines.append(f"[系统恢复] 离线期间 {total} 个事件：")

    # List-mode items (truncate at _LIST_ITEM_LIMIT)
    shown = list_items[:_LIST_ITEM_LIMIT]
    for evt in shown:
        hhmm = _extract_hhmm(evt.get("wall_time", "T00:00:00Z"))
        tag = evt.get("tag", "事件")
        hint = evt.get("text_hint", "")
        lines.append(f"- [{hhmm}] {tag} {hint}")

    overflow = len(list_items) - _LIST_ITEM_LIMIT
    if overflow > 0:
        # Find tag of overflowed items for description
        overflow_tags: dict[str, int] = defaultdict(int)
        for evt in list_items[_LIST_ITEM_LIMIT:]:
            overflow_tags[evt.get("tag") or "事件"] += 1
        overflow_parts = [f"{n} 条{t}" for t, n in overflow_tags.items()]
        lines.append(f"另有 {overflow} 条（{'、'.join(overflow_parts)}）")

    # Count-mode summary
    if count_buckets:
        count_parts = [f"{n} 条{tag}" for tag, n in count_buckets.items()]
        count_text = "、".join(count_parts)
        # If there were also list items, prefix with "另有"
        if list_items:
            lines.append(f"另有 {count_text}（略）")
        else:
            lines.append(f"{count_text}（略）")

    lines.append("请查看当前市况，评估是否需要补充分析。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery statistics for briefing injection
# ---------------------------------------------------------------------------


@dataclass
class DeliveryStats:
    """Aggregated delivery statistics for a time window."""

    total: int
    delivered: int
    undelivered: int
    by_tag: dict[str, dict] = field(default_factory=dict)

    def format_report(self) -> str:
        """One-line health text for briefing injection."""
        if self.total == 0:
            return "系统健康：过去24h 无事件推送。"
        if self.undelivered == 0:
            return (
                f"系统健康：过去24h 推送 {self.total} 个事件，"
                f"{self.delivered} 个已处理。"
            )
        return (
            f"系统健康：过去24h 推送 {self.total} 个事件，"
            f"{self.delivered} 个已处理，{self.undelivered} 个未送达。"
        )
