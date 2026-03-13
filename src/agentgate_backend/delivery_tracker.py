"""Delivery tracking for injected messages.

Tracks pending deliveries and confirms them when JSONL user entries appear.
Used by SessionMonitor to detect delivery timeouts and trigger recovery.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    TIMEOUT = "timeout"


@dataclass
class PendingDelivery:
    delivery_id: str
    window_id: str
    text_hint: str
    sent_at: float  # monotonic
    sent_at_iso: str  # wall clock ISO for API response
    timeout: float
    status: DeliveryStatus = DeliveryStatus.PENDING
    confirmed_at: str | None = None


class DeliveryTracker:
    """Track message delivery from injection to JSONL confirmation."""

    def __init__(self) -> None:
        self._deliveries: dict[str, PendingDelivery] = {}

    def register(
        self, window_id: str, text: str, *, timeout: float = 30.0
    ) -> str:
        delivery_id = uuid.uuid4().hex[:12]
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        self._deliveries[delivery_id] = PendingDelivery(
            delivery_id=delivery_id,
            window_id=window_id,
            text_hint=text[:50],
            sent_at=time.monotonic(),
            sent_at_iso=now_iso,
            timeout=timeout,
        )
        logger.debug(
            "Delivery registered: id=%s window=%s text=%s",
            delivery_id, window_id, text[:50],
        )
        return delivery_id

    def get_status(self, delivery_id: str) -> dict | None:
        d = self._deliveries.get(delivery_id)
        if d is None:
            return None
        return {
            "delivery_id": d.delivery_id,
            "status": d.status.value,
            "sent_at": d.sent_at_iso,
            "confirmed_at": d.confirmed_at,
        }

    def confirm_for_window(self, window_id: str) -> list[str]:
        """Confirm all pending deliveries for a window.

        Called when SessionMonitor detects a new user message in JSONL.
        Returns list of confirmed delivery_ids.
        """
        confirmed = []
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        for d in self._deliveries.values():
            if d.window_id == window_id and d.status == DeliveryStatus.PENDING:
                d.status = DeliveryStatus.CONFIRMED
                d.confirmed_at = now_iso
                confirmed.append(d.delivery_id)
                logger.info(
                    "Delivery confirmed: id=%s window=%s",
                    d.delivery_id, window_id,
                )
        return confirmed

    def check_timeouts(self) -> list[dict]:
        """Check for timed-out deliveries. Returns list of timeout info dicts."""
        now = time.monotonic()
        timed_out = []
        for d in self._deliveries.values():
            if d.status == DeliveryStatus.PENDING and (now - d.sent_at) > d.timeout:
                d.status = DeliveryStatus.TIMEOUT
                timed_out.append({
                    "delivery_id": d.delivery_id,
                    "window_id": d.window_id,
                    "text_hint": d.text_hint,
                })
                logger.warning(
                    "Delivery timeout: id=%s window=%s text=%s",
                    d.delivery_id, d.window_id, d.text_hint,
                )
        return timed_out

    def pending_count(self, window_id: str) -> int:
        return sum(
            1 for d in self._deliveries.values()
            if d.window_id == window_id and d.status == DeliveryStatus.PENDING
        )

    def cleanup_old(self, max_age: float = 300.0, pending_max_age: float = 600.0) -> None:
        """Remove old deliveries to prevent memory leaks.

        - Confirmed/timeout deliveries: removed after max_age (default 5min)
        - Stale PENDING deliveries: removed after pending_max_age (default 10min)
          These are deliveries that never got confirmed or timed out (e.g. bug).
        """
        now = time.monotonic()
        to_remove = [
            did for did, d in self._deliveries.items()
            if (d.status != DeliveryStatus.PENDING and (now - d.sent_at) > max_age)
            or (d.status == DeliveryStatus.PENDING and (now - d.sent_at) > pending_max_age)
        ]
        for did in to_remove:
            del self._deliveries[did]
