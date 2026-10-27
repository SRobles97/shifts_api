"""
Repository for the bulk regular-schedule write ("Editar turno habitual" for
several machines): one `(device_id, shift_type)` at a time.

Takes the caller's `asyncpg.Connection`, already inside a transaction, so all
machines and the single outbox event commit together.

Vigencia rules (spec decision 5), for a new schedule starting on `valid_from`:
- (a) a row covers `valid_from`: it is updated in place when it starts that
  same day; otherwise it ends the day before and the new row inherits its end,
  bounded by the next row's start. kind "split".
- (b) nothing covers it but a later row exists: the new row ends the day
  before that row starts. kind "future_bounded".
- (c) nothing at all: the new row is open-ended. kind "new_coverage".
Rows that start after `valid_from` are never touched.
"""

from datetime import date, timedelta
from typing import Any, Dict, Optional

import asyncpg

_ONE_DAY = timedelta(days=1)


def _min_date(a: Optional[date], b: Optional[date]) -> Optional[date]:
    """Earliest of two optional end dates; None means open-ended."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _jsonb_text(row: Optional[asyncpg.Record], column: str) -> Optional[str]:
    # no jsonb codec on the pool: asyncpg hands jsonb columns back as text
    if row is None:
        return None
    return row[column]


class BulkSchedulesCRUD:
    """Writes a new regular schedule from a date for one machine and shift."""

    @staticmethod
    async def plan(
        conn: asyncpg.Connection, device_id: int, shift_type: str, valid_from: date
    ) -> Dict[str, Any]:
        """Row covering `valid_from` (row-locked), next row's start and the outcome.

        FOR UPDATE: must be called inside a transaction.
        """
        # Not SELECT *: an open valid_range ends past year 9999, which asyncpg
        # cannot decode.
        cover = await conn.fetchrow(
            """
            SELECT id, device_id, shift_type, day_schedules, extra_hours, special_days,
                   valid_from, valid_to, version, source
            FROM device_schedules
            WHERE device_id = $1 AND shift_type = $2 AND valid_range @> $3::date
            FOR UPDATE;
            """,
            device_id,
            shift_type,
            valid_from,
        )
        next_from = await conn.fetchval(
            """
            SELECT valid_from
            FROM device_schedules
            WHERE device_id = $1 AND shift_type = $2 AND valid_from > $3::date
            ORDER BY valid_from
            LIMIT 1;
            """,
            device_id,
            shift_type,
            valid_from,
        )
        bound = next_from - _ONE_DAY if next_from is not None else None
        if cover is not None:
            kind = "split"
            new_valid_to = _min_date(cover["valid_to"], bound)
        elif next_from is not None:
            kind = "future_bounded"
            new_valid_to = bound
        else:
            kind = "new_coverage"
            new_valid_to = None
        return {"cover": cover, "new_valid_to": new_valid_to, "kind": kind}

    @staticmethod
    async def apply_for_device(
        conn: asyncpg.Connection,
        device_id: int,
        shift_type: str,
        valid_from: date,
        day_schedules_json: str,
        extra_hours_json: Optional[str],
    ) -> Dict[str, Any]:
        """Write the new regular schedule; `extra_hours_json=None` keeps the cover's extras.

        Returns {"before_row", "new_row_id", "new_valid_to", "kind"}.
        """
        plan = await BulkSchedulesCRUD.plan(conn, device_id, shift_type, valid_from)
        cover = plan["cover"]
        new_valid_to = plan["new_valid_to"]

        if cover is not None and cover["valid_from"] == valid_from:
            new_row_id = await conn.fetchval(
                """
                UPDATE device_schedules
                SET day_schedules = $2::jsonb,
                    extra_hours = CASE WHEN $3::boolean THEN $4::jsonb ELSE extra_hours END,
                    source = 'ui',
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id;
                """,
                cover["id"],
                day_schedules_json,
                extra_hours_json is not None,
                extra_hours_json,
            )
        else:
            if cover is not None:
                # Shrink first: the exclusion constraint is not deferrable.
                await conn.execute(
                    """
                    UPDATE device_schedules
                    SET valid_to = $2::date - INTERVAL '1 day',
                        updated_at = NOW()
                    WHERE id = $1;
                    """,
                    cover["id"],
                    valid_from,
                )
            # The new row carries the cover's recurring holidays and, unless
            # replaced, its extra hours.
            new_row_id = await conn.fetchval(
                """
                INSERT INTO device_schedules (
                    device_id, shift_type, day_schedules, extra_hours, special_days,
                    valid_from, valid_to, version, source, updated_at
                )
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7, $8, 'ui', NOW())
                RETURNING id;
                """,
                device_id,
                shift_type,
                day_schedules_json,
                extra_hours_json if extra_hours_json is not None else _jsonb_text(cover, "extra_hours"),
                _jsonb_text(cover, "special_days"),
                valid_from,
                new_valid_to,
                cover["version"] if cover is not None else "1.0",
            )

        return {
            "before_row": cover,
            "new_row_id": new_row_id,
            "new_valid_to": new_valid_to,
            "kind": plan["kind"],
        }


bulk_schedules_crud = BulkSchedulesCRUD()
apply_for_device = bulk_schedules_crud.apply_for_device
