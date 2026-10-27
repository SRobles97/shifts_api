"""
Repository over `device_schedule_exceptions` and the schedule SQL functions
`device_schedule_blocks` / `device_schedule_days`.

Methods that write take the caller's `asyncpg.Connection` (already inside a
transaction) so the exception, its outbox event and the jsonb cleanup commit
together. Plain reads take a pool; `blocks`/`days` accept either, since both
Pool and Connection expose `fetch`.
"""

import json
from datetime import date
from typing import Dict, List, Optional, Set
from uuid import UUID

import asyncpg

from ..models.notification_event import DeviceRef
from ..schemas.day_changes import ChangeSpec


class ExceptionsCRUD:
    """Per-date schedule exceptions and the effective-schedule functions."""

    @staticmethod
    async def device_refs(pool: asyncpg.Pool, device_ids: List[int]) -> Dict[int, DeviceRef]:
        """Resolve company and naming for many devices in a single query."""
        rows = await pool.fetch(
            """
            SELECT id, company_id, device_key, display_name
            FROM devices
            WHERE id = ANY($1::bigint[]);
            """,
            device_ids,
        )
        return {
            r["id"]: DeviceRef(
                id=r["id"],
                company_id=r["company_id"],
                key=r["device_key"] or "",
                display_name=r["display_name"] or "",
            )
            for r in rows
        }

    @staticmethod
    async def timezones(pool_or_conn, device_ids: List[int]) -> Dict[int, str]:
        """IANA zone per device, the one the schedule functions resolve times in."""
        rows = await pool_or_conn.fetch(
            """
            SELECT id, COALESCE(timezone, 'America/Santiago') AS tz
            FROM devices
            WHERE id = ANY($1::bigint[]);
            """,
            device_ids,
        )
        return {r["id"]: r["tz"] for r in rows}

    @staticmethod
    async def mirrored_ids(pool_or_conn, device_ids: List[int]) -> Set[int]:
        """Devices that have at least one schedule row mirrored from a pilot."""
        rows = await pool_or_conn.fetch(
            """
            SELECT d AS device_id
            FROM unnest($1::bigint[]) AS d
            WHERE EXISTS (
                SELECT 1 FROM device_schedules s
                WHERE s.device_id = d AND s.source = 'pilot_mirror'
            );
            """,
            device_ids,
        )
        return {r["device_id"] for r in rows}

    @staticmethod
    async def get_many(
        conn: asyncpg.Connection, device_ids: List[int], shift_type: str, day: date
    ) -> Dict[int, asyncpg.Record]:
        """Exception in force per device for one shift and date, row-locked.

        FOR UPDATE: must be called inside a transaction.
        """
        rows = await conn.fetch(
            """
            SELECT *
            FROM device_schedule_exceptions
            WHERE device_id = ANY($1::bigint[]) AND shift_type = $2 AND date = $3
            FOR UPDATE;
            """,
            device_ids,
            shift_type,
            day,
        )
        return {r["device_id"]: r for r in rows}

    @staticmethod
    async def upsert(
        conn: asyncpg.Connection,
        device_id: int,
        shift_type: str,
        day: date,
        change: ChangeSpec,
        operation_id: UUID,
    ) -> int:
        """Create the exception or replace the one already on (device, shift, date)."""
        # camelCase keys: the SQL function reads durationMinutes.
        work_hours = (
            json.dumps({"start": change.work_hours.start, "end": change.work_hours.end})
            if change.work_hours
            else None
        )
        breaks = (
            json.dumps(
                [{"start": b.start, "durationMinutes": b.duration_minutes} for b in change.breaks]
            )
            if change.breaks
            else None
        )
        return await conn.fetchval(
            """
            INSERT INTO device_schedule_exceptions (
                device_id, shift_type, date, kind, work_hours, breaks,
                extra_minutes, extra_position, reason, operation_id, source,
                created_by
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, 'ui', NULL)
            ON CONFLICT (device_id, shift_type, date) DO UPDATE SET
                kind = EXCLUDED.kind,
                work_hours = EXCLUDED.work_hours,
                breaks = EXCLUDED.breaks,
                extra_minutes = EXCLUDED.extra_minutes,
                extra_position = EXCLUDED.extra_position,
                reason = EXCLUDED.reason,
                operation_id = EXCLUDED.operation_id,
                source = 'ui',
                updated_at = now()
            RETURNING id;
            """,
            device_id,
            shift_type,
            day,
            change.kind,
            work_hours,
            breaks,
            change.minutes,
            change.position,
            change.reason or "",
            operation_id,
        )

    @staticmethod
    async def strip_jsonb_key(conn: asyncpg.Connection, device_id: int, day: date) -> int:
        """Remove the exact-date key from special_days in every schedule row of a device."""
        status = await conn.execute(
            """
            UPDATE device_schedules
            SET special_days = special_days - $2::text
            WHERE device_id = $1 AND special_days ? $2::text;
            """,
            device_id,
            day.isoformat(),
        )
        return int(status.rsplit(" ", 1)[-1])

    @staticmethod
    async def get(pool: asyncpg.Pool, exception_id: int) -> Optional[asyncpg.Record]:
        return await pool.fetchrow(
            "SELECT * FROM device_schedule_exceptions WHERE id = $1;", exception_id
        )

    @staticmethod
    async def delete(conn: asyncpg.Connection, exception_id: int) -> Optional[asyncpg.Record]:
        return await conn.fetchrow(
            "DELETE FROM device_schedule_exceptions WHERE id = $1 RETURNING *;", exception_id
        )

    @staticmethod
    async def list(
        pool: asyncpg.Pool, device_ids: List[int], d_from: date, d_to: date
    ) -> List[asyncpg.Record]:
        return await pool.fetch(
            """
            SELECT *
            FROM device_schedule_exceptions
            WHERE device_id = ANY($1::bigint[]) AND date BETWEEN $2 AND $3
            ORDER BY date, device_id, shift_type;
            """,
            device_ids,
            d_from,
            d_to,
        )

    @staticmethod
    async def blocks(conn_or_pool, device_ids: List[int], d_from: date, d_to: date) -> List[asyncpg.Record]:
        """Effective work blocks, exceptions included."""
        return await conn_or_pool.fetch(
            "SELECT * FROM device_schedule_blocks($1::bigint[], $2, $3);",
            device_ids,
            d_from,
            d_to,
        )

    @staticmethod
    async def days(conn_or_pool, device_ids: List[int], d_from: date, d_to: date) -> List[asyncpg.Record]:
        """Dates with scheduled work per device and shift, and where each came from."""
        return await conn_or_pool.fetch(
            "SELECT * FROM device_schedule_days($1::bigint[], $2, $3);",
            device_ids,
            d_from,
            d_to,
        )

    @staticmethod
    async def exceptions_from(
        conn: asyncpg.Connection, device_ids: List[int], day_from: date
    ) -> List[asyncpg.Record]:
        """Exceptions on or after a date; what a regular-schedule edit would leave in place."""
        return await conn.fetch(
            """
            SELECT *
            FROM device_schedule_exceptions
            WHERE device_id = ANY($1::bigint[]) AND date >= $2
            ORDER BY device_id, date, shift_type;
            """,
            device_ids,
            day_from,
        )


exceptions_crud = ExceptionsCRUD()
