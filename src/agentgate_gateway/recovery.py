"""Crash recovery manager for AgentGate Gateway."""

import logging

from agentgate_gateway.db import MessageDB

logger = logging.getLogger(__name__)

# Messages with retry_count >= this threshold are considered permanently
# undeliverable and skipped during startup recovery.
MAX_RECOVERY_RETRIES = 15


class RecoveryManager:
    def __init__(self, db: MessageDB, inject_fn, repush_fn):
        """
        inject_fn: async callable that re-injects an inbound message dict
        repush_fn: async callable that re-pushes an outbound message dict
        """
        self._db = db
        self._inject = inject_fn
        self._repush = repush_fn

    async def recover_on_startup(self):
        """Gateway startup recovery — compensate for pending/failed messages."""
        # 1. Pending inbound → re-inject
        pending_in = await self._db.get_pending_inbound()
        logger.info("Startup recovery: %d pending inbound messages", len(pending_in))
        for msg in pending_in:
            try:
                await self._inject(msg)
            except Exception as e:
                logger.error("Failed to reinject %s: %s", msg["id"], e, exc_info=True)

        # 2. Pending outbound → re-push
        pending_out = await self._db.get_pending_outbound()
        logger.info("Startup recovery: %d pending outbound messages", len(pending_out))
        for msg in pending_out:
            try:
                await self._repush(msg)
            except Exception as e:
                logger.error("Failed to repush %s: %s", msg["id"], e, exc_info=True)

        # 3. Failed outbound → try again (channel may have recovered)
        #    Skip messages that exceeded retry threshold — permanently undeliverable.
        failed_out = await self._db.get_failed_outbound()
        retryable = [m for m in failed_out if m.get("retry_count", 0) < MAX_RECOVERY_RETRIES]
        skipped = len(failed_out) - len(retryable)
        if skipped:
            logger.info(
                "Startup recovery: skipping %d permanently failed outbound messages (retry_count >= %d)",
                skipped, MAX_RECOVERY_RETRIES,
            )
        logger.info("Startup recovery: %d failed outbound messages to retry", len(retryable))
        for msg in retryable:
            try:
                await self._repush(msg)
            except Exception as e:
                logger.error("Failed to repush failed %s: %s", msg["id"], e, exc_info=True)

    async def on_backend_recovered(self, backend_id: str):
        """Backend unhealthy→healthy: re-inject unprocessed + failed messages (AC-21/22)."""
        # 1. Re-inject delivered-but-unprocessed messages
        unprocessed = await self._db.get_unprocessed_for_backend(backend_id)
        logger.info(
            "Backend %s recovered, %d unprocessed messages to reinject",
            backend_id,
            len(unprocessed),
        )
        for msg in unprocessed:
            try:
                await self._db.update_inbound_process(msg["id"], "reinjected")
                await self._inject(msg)
            except Exception as e:
                logger.error(
                    "Failed to reinject %s for backend %s: %s",
                    msg["id"],
                    backend_id,
                    e,
                    exc_info=True,
                )

        # 2. Re-inject failed messages (delivery retries exhausted while backend was down)
        failed = await self._db.get_failed_inbound_for_backend(backend_id)
        if failed:
            logger.info(
                "Backend %s recovered, %d failed messages to retry",
                backend_id,
                len(failed),
            )
        for msg in failed:
            try:
                # Reset delivery status so reinject can update it properly
                await self._db.update_inbound_delivery(msg["id"], "pending")
                await self._inject(msg)
            except Exception as e:
                logger.error(
                    "Failed to reinject failed msg %s for backend %s: %s",
                    msg["id"],
                    backend_id,
                    e,
                    exc_info=True,
                )
