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
                    device_id, day_schedules, extra_hours, special_days,
                    valid_from, valid_to, version, source, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                RETURNING id;
                """,
                schedule_data["device_id"],
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
        for the same device, then inserts the new schedule.

        Returns:
            ID of the created schedule
        """
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Close previous open-ended schedule
                new_valid_from = schedule_data["valid_from"]
                await conn.execute(
                    """
                    UPDATE device_schedules
                    SET valid_to = $2::date - INTERVAL '1 day',
                        updated_at = NOW()
                    WHERE device_id = $1
                      AND valid_to IS NULL
                      AND valid_from < $2::date;
                    """,
                    schedule_data["device_id"],
                    new_valid_from,
                )

                # Insert new schedule
                result = await conn.fetchval(
                    """
                    INSERT INTO device_schedules (
                        device_id, day_schedules, extra_hours, special_days,
                        valid_from, valid_to, version, source, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                    RETURNING id;
                    """,
                    schedule_data["device_id"],
                    schedule_data["day_schedules"],
                    schedule_data.get("extra_hours"),
                    schedule_data.get("special_days"),
                    new_valid_from,
                    schedule_data.get("valid_to"),
                    schedule_data.get("version", "1.0"),
                    schedule_data.get("source", "ui"),
                )

                logger.info(
                    f"Schedule created with auto-close for device_id={schedule_data['device_id']} (id={result})"
                )
                return result

    @staticmethod
    async def get_by_id(pool: asyncpg.Pool, schedule_id: int) -> Optional[asyncpg.Record]:
        """Get a schedule by its primary key."""
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.id = $1;
                """,
                schedule_id,
            )

    @staticmethod
    async def get_current_by_device_id(pool: asyncpg.Pool, device_id: int) -> Optional[asyncpg.Record]:
        """
        Get the currently effective schedule for a device (valid_range @> CURRENT_DATE).
        """
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.valid_range @> CURRENT_DATE;
                """,
                device_id,
            )

    @staticmethod
    async def get_by_device_id_and_date(
        pool: asyncpg.Pool, device_id: int, target_date: date
    ) -> Optional[asyncpg.Record]:
        """Get the schedule for a device effective on a specific date."""
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                  AND s.valid_range @> $2::date;
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
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1
                ORDER BY s.valid_from DESC;
                """,
                device_id,
            )

    @staticmethod
    async def get_all_current(pool: asyncpg.Pool) -> List[asyncpg.Record]:
        """Get all currently effective schedules (one per device)."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.valid_range @> CURRENT_DATE
                ORDER BY s.device_id;
                """
            )

    @staticmethod
    async def get_by_day(pool: asyncpg.Pool, day: str) -> List[asyncpg.Record]:
        """Get all currently effective schedules that include a specific day."""
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.valid_from, s.valid_to, s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM device_schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.day_schedules ? $1
                  AND s.valid_range @> CURRENT_DATE
                ORDER BY s.device_id;
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
    async def delete_current_by_device_id(pool: asyncpg.Pool, device_id: int) -> bool:
        """Delete the currently effective schedule for a device."""
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM device_schedules
                WHERE device_id = $1
                  AND valid_range @> CURRENT_DATE;
                """,
                device_id,
            )
            deleted_count = int(result.split()[-1])
            logger.info(f"Current schedule for device_id={device_id} deleted: {deleted_count > 0}")
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
    async def get_special_days(pool: asyncpg.Pool, device_id: int) -> Optional[Dict[str, Any]]:
        """Get special_days JSONB for the current schedule of a device."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT special_days FROM device_schedules
                WHERE device_id = $1
                  AND valid_range @> CURRENT_DATE;
                """,
                device_id,
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
