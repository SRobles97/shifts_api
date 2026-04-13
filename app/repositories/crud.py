"""
CRUD operations for device_schedules.

This module contains all database operations for schedule management,
providing a clean interface between the service layer and the database.

All methods accept an asyncpg.Pool as the first parameter (injected via dependency).
"""

from typing import List, Optional, Dict, Any
from datetime import date

import asyncpg
import json
from loguru import logger


class ScheduleCRUD:
    """CRUD operations for schedule management (N schedules per device with date ranges)."""

    @staticmethod
    async def insert(pool: asyncpg.Pool, schedule_data: Dict[str, Any]) -> int:
        """
        Insert a new schedule for a device.

        Returns:
            ID of the created schedule
        """
        async with pool.acquire() as conn:
            result = await conn.fetchval(
                """
                INSERT INTO device_schedules (
                    device_id, shift_type, day_schedules, extra_hours, special_days,
                    valid_from, valid_to, version, source, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                RETURNING id;
                """,
                schedule_data["device_id"],
                schedule_data.get("shift_type", "day"),
                schedule_data["day_schedules"],
                schedule_data.get("extra_hours"),
                schedule_data.get("special_days"),
                schedule_data["valid_from"],
                schedule_data.get("valid_to"),
                schedule_data.get("version", "1.0"),
                schedule_data.get("source", "ui"),
            )

            logger.info(f"Schedule inserted for device_id={schedule_data['device_id']} (id={result})")
            return result

    @staticmethod
    async def create_with_auto_close(pool: asyncpg.Pool, schedule_data: Dict[str, Any]) -> int:
        """
        Atomically close the previous open-ended schedule and insert a new one.

        Sets valid_to = new_valid_from - 1 day on the previous open-ended schedule
        for the same device and shift_type, then inserts the new schedule.

        Returns:
            ID of the created schedule
        """
        shift_type = schedule_data.get("shift_type", "day")
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Close previous open-ended schedule (scoped by shift_type)
                new_valid_from = schedule_data["valid_from"]
                await conn.execute(
                    """
                    UPDATE device_schedules
                    SET valid_to = $2::date - INTERVAL '1 day',
                        updated_at = NOW()
                    WHERE device_id = $1
                      AND shift_type = $3
                      AND valid_to IS NULL
                      AND valid_from < $2::date;
                    """,
                    schedule_data["device_id"],
                    new_valid_from,
                    shift_type,
                )

                # Insert new schedule
                result = await conn.fetchval(
                    """
                    INSERT INTO device_schedules (
                        device_id, shift_type, day_schedules, extra_hours, special_days,
                        valid_from, valid_to, version, source, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                    RETURNING id;
                    """,
                    schedule_data["device_id"],
                    shift_type,
                    schedule_data["day_schedules"],
                    schedule_data.get("extra_hours"),
                    schedule_data.get("special_days"),
                    new_valid_from,
                    schedule_data.get("valid_to"),
                    schedule_data.get("version", "1.0"),
                    schedule_data.get("source", "ui"),
                )

                logger.info(
                    f"Schedule created with auto-close for device_id={schedule_data['device_id']} "
                    f"shift_type={shift_type} (id={result})"
                )
                return result

    @staticmethod
    async def create_with_split(pool: asyncpg.Pool, schedule_data: Dict[str, Any]) -> int:
        """
        Insert a bounded schedule, splitting any overlapping schedule around it.

        If an existing schedule overlaps the new [valid_from, valid_to] range
        for the same (device_id, shift_type), it is split into up to two parts:
        a "before" portion and an "after" portion that preserves the original config.

        If no overlap exists, the schedule is inserted normally.

        Returns:
            ID of the created schedule
        """
        device_id = schedule_data["device_id"]
        shift_type = schedule_data.get("shift_type", "day")
        new_from = schedule_data["valid_from"]
        new_to = schedule_data["valid_to"]

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Find existing schedule that overlaps the new range
                existing = await conn.fetchrow(
                    """
                    SELECT id, day_schedules, extra_hours, special_days,
                           valid_from, valid_to, version, source
                    FROM device_schedules
                    WHERE device_id = $1
                      AND shift_type = $2
                      AND valid_range && daterange($3::date, $4::date, '[]')
                    ORDER BY valid_from
                    LIMIT 1;
                    """,
                    device_id, shift_type, new_from, new_to,
                )

                if existing is not None:
                    ex_from = existing["valid_from"]
                    ex_to = existing["valid_to"]

                    has_before = new_from > ex_from
                    has_after = ex_to is None or new_to < ex_to

                    if has_before:
                        # Shrink existing to end the day before the override
                        await conn.execute(
                            """
                            UPDATE device_schedules
                            SET valid_to = $2::date - INTERVAL '1 day',
                                updated_at = NOW()
                            WHERE id = $1;
                            """,
                            existing["id"], new_from,
                        )
                    elif has_after:
                        # Override starts on same day as existing: push existing forward
                        await conn.execute(
                            """
                            UPDATE device_schedules
                            SET valid_from = $2::date + INTERVAL '1 day',
                                updated_at = NOW()
                            WHERE id = $1;
                            """,
                            existing["id"], new_to,
                        )
                    else:
                        # Override covers entire existing range: delete it
                        await conn.execute(
                            "DELETE FROM device_schedules WHERE id = $1;",
                            existing["id"],
                        )

                    # Clone the "after" portion (only if we shrunk the before)
                    if has_before and has_after:
                        await conn.execute(
                            """
                            INSERT INTO device_schedules (
                                device_id, shift_type, day_schedules, extra_hours,
                                special_days, valid_from, valid_to, version, source,
                                updated_at
                            )
                            VALUES ($1, $2, $3, $4, $5,
                                    $6::date + INTERVAL '1 day', $7,
                                    $8, $9, NOW());
                            """,
                            device_id, shift_type,
                            existing["day_schedules"], existing["extra_hours"],
                            existing["special_days"],
                            new_to, ex_to,
                            existing["version"], existing["source"],
                        )

                # Insert the override schedule
                result = await conn.fetchval(
                    """
                    INSERT INTO device_schedules (
                        device_id, shift_type, day_schedules, extra_hours,
                        special_days, valid_from, valid_to, version, source,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                    RETURNING id;
                    """,
                    device_id, shift_type,
                    schedule_data["day_schedules"],
                    schedule_data.get("extra_hours"),
                    schedule_data.get("special_days"),
                    new_from, new_to,
                    schedule_data.get("version", "1.0"),
                    schedule_data.get("source", "ui"),
                )

                logger.info(
                    f"Schedule created with split for device_id={device_id} "
                    f"shift_type={shift_type} (id={result})"
                )
                return result

    @staticmethod
    async def get_by_id(pool: asyncpg.Pool, schedule_id: int) -> Optional[asyncpg.Record]:
        """Get a schedule by its primary key."""
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.id = $1;
                """,
                schedule_id,
            )

    @staticmethod
    async def get_current_by_device_id(
        pool: asyncpg.Pool, device_id: int, shift_type: str = "day",
    ) -> Optional[asyncpg.Record]:
        """
        Get the currently effective schedule for a device and shift_type
        (valid_range @> CURRENT_DATE).
        """
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.shift_type = $2
                  AND s.valid_range @> CURRENT_DATE;
                """,
                device_id,
                shift_type,
            )

    @staticmethod
    async def get_all_current_by_device_id(
        pool: asyncpg.Pool, device_id: int,
    ) -> List[asyncpg.Record]:
        """
        Get all currently effective schedules for a device (all shift types).
        """
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.valid_range @> CURRENT_DATE
                ORDER BY s.shift_type;
                """,
                device_id,
            )

    @staticmethod
    async def get_by_device_id_and_date(
        pool: asyncpg.Pool, device_id: int, target_date: date, shift_type: str = "day",
    ) -> Optional[asyncpg.Record]:
        """Get the schedule for a device effective on a specific date (single shift type)."""
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.shift_type = $3
                  AND s.valid_range @> $2::date;
                """,
                device_id,
                target_date,
                shift_type,
            )

    @staticmethod
    async def get_all_by_device_id_and_date(
        pool: asyncpg.Pool, device_id: int, target_date: date,
    ) -> List[asyncpg.Record]:
        """Get all schedules for a device effective on a specific date (all shift types)."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.valid_range @> $2::date
                ORDER BY s.shift_type;
                """,
                device_id,
                target_date,
            )

    @staticmethod
    async def get_all_by_device_id(pool: asyncpg.Pool, device_id: int) -> List[asyncpg.Record]:
        """Get all schedules (history) for a device, ordered by valid_from DESC."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                ORDER BY s.valid_from DESC, s.shift_type;
                """,
                device_id,
            )

    @staticmethod
    async def get_all_current(pool: asyncpg.Pool) -> List[asyncpg.Record]:
        """Get all currently effective schedules (may return multiple per device if day+night)."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.valid_range @> CURRENT_DATE
                ORDER BY s.device_id, s.shift_type;
                """
            )

    @staticmethod
    async def get_by_day(pool: asyncpg.Pool, day: str) -> List[asyncpg.Record]:
        """Get all currently effective schedules that include a specific day."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.shift_type, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.day_schedules ? $1
                  AND s.valid_range @> CURRENT_DATE
                ORDER BY s.device_id, s.shift_type;
                """,
                day,
            )

    @staticmethod
    async def partial_update(
        pool: asyncpg.Pool,
        schedule_id: int,
        update_data: Dict[str, Any],
    ) -> bool:
        """
        Partially update a schedule by its primary key.

        Returns:
            True if schedule was updated, False if not found
        """
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM device_schedules WHERE id = $1",
                schedule_id,
            )
            if not existing:
                return False

            update_fields = []
            values = []
            param_idx = 2  # $1 is schedule_id

            for field, value in update_data.items():
                update_fields.append(f"{field} = ${param_idx}")
                values.append(value)
                param_idx += 1

            if not update_fields:
                return True

            update_fields.append("updated_at = NOW()")

            query = f"""
                UPDATE device_schedules
                SET {', '.join(update_fields)}
                WHERE id = $1
            """

            await conn.execute(query, schedule_id, *values)
            logger.info(f"Schedule id={schedule_id} partially updated")
            return True

    @staticmethod
    async def delete_current_by_device_id(
        pool: asyncpg.Pool, device_id: int, shift_type: str = "day",
    ) -> bool:
        """Delete the currently effective schedule for a device and shift_type."""
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM device_schedules
                WHERE device_id = $1
                  AND shift_type = $2
                  AND valid_range @> CURRENT_DATE;
                """,
                device_id,
                shift_type,
            )
            deleted_count = int(result.split()[-1])
            logger.info(f"Current schedule for device_id={device_id} shift_type={shift_type} deleted: {deleted_count > 0}")
            return deleted_count > 0

    @staticmethod
    async def delete_by_id(pool: asyncpg.Pool, schedule_id: int) -> bool:
        """Delete a specific schedule by its primary key."""
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM device_schedules WHERE id = $1",
                schedule_id,
            )
            deleted_count = int(result.split()[-1])
            logger.info(f"Schedule id={schedule_id} deleted: {deleted_count > 0}")
            return deleted_count > 0

    @staticmethod
    async def get_special_days(
        pool: asyncpg.Pool, device_id: int, shift_type: str = "day",
    ) -> Optional[Dict[str, Any]]:
        """Get special_days JSONB for the current schedule of a device and shift_type."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT special_days FROM device_schedules
                WHERE device_id = $1
                  AND shift_type = $2
                  AND valid_range @> CURRENT_DATE;
                """,
                device_id,
                shift_type,
            )
            if row is None:
                return None

            data = row["special_days"]
            if not data:
                return {}
            if isinstance(data, str):
                return json.loads(data)
            return data

    @staticmethod
    async def get_device_id_by_name(pool: asyncpg.Pool, device_name: str) -> Optional[int]:
        """
        Look up a device ID by name.

        Searches across device_key, display_name, and device_code
        to match flexibly.
        """
        async with pool.acquire() as conn:
            # Exact match on device_key
            row = await conn.fetchrow(
                "SELECT id FROM devices WHERE device_key = $1",
                device_name,
            )
            if row:
                return row["id"]

            # Exact match on display_name
            row = await conn.fetchrow(
                "SELECT id FROM devices WHERE display_name = $1",
                device_name,
            )
            if row:
                return row["id"]

            # Partial match on display_name
            row = await conn.fetchrow(
                "SELECT id FROM devices WHERE display_name ILIKE '%' || $1 || '%'",
                device_name,
            )
            if row:
                return row["id"]

            return None


schedule_crud = ScheduleCRUD()
