"""
Writes to `notification_outbox`.

Kept apart from `ScheduleCRUD` because it is a different table with a different
owner: `shifts_api` only ever inserts here, and `notify_api` is what reads and
drains the queue.

Every method takes a live `asyncpg.Connection` rather than a pool — the whole
point of the outbox is that the event lands in the *same transaction* as the
schedule change, so a mail outage can never roll back a schedule save and a
rolled-back schedule save can never leave a phantom notification behind.
"""

import json
from typing import Any

import asyncpg
from loguru import logger

from ..models.notification_event import OutboxEvent


class NotificationOutboxCRUD:
    """Insert-only access to the notification outbox."""

    @staticmethod
    async def insert(conn: asyncpg.Connection, event: OutboxEvent) -> None:
        """Enqueue one event on the caller's open transaction.

        `ON CONFLICT DO NOTHING` on `dedupe_key` makes a replayed request
        harmless: the second insert is a no-op instead of an error that would
        take the schedule write down with it.
        """
        await conn.execute(
            """
            INSERT INTO notification_outbox (type_key, company_id, dedupe_key, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (dedupe_key) DO NOTHING;
            """,
            event.type_key,
            event.company_id,
            event.dedupe_key,
            json.dumps(event.payload),
        )
        logger.info(
            f"Notification queued type={event.type_key} "
            f"company_id={event.company_id} dedupe_key={event.dedupe_key}"
        )

    @staticmethod
    async def insert_if_present(conn: asyncpg.Connection, event: Any) -> None:
        """Insert when an event was built, do nothing when it was None.

        Lets every mutating CRUD path share one unconditional call site instead
        of repeating the same `if event is not None` guard six times.
        """
        if event is not None:
            await NotificationOutboxCRUD.insert(conn, event)


notification_outbox_crud = NotificationOutboxCRUD()
