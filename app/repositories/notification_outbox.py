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

        `ON CONFLICT DO NOTHING` on `dedupe_key` keeps a duplicate insert from
        failing the schedule write it rides along with.

        It does NOT make a replayed HTTP request idempotent, despite how easy
        that is to assume. `changed_at` is taken fresh per request and is part of
        the key, so a retried POST/PUT builds a different key and inserts a
        second row. What actually stops a retried PUT/PATCH from mailing twice is
        upstream: `build_shift_change_event` returns None when the diff is empty,
        and a replay of a write that already landed has an empty diff. A replayed
        POST is a genuine second schedule (create auto-closes the previous one),
        so it notifies on purpose. Real cross-request idempotency would need a
        client-supplied token, which this API does not have.
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
