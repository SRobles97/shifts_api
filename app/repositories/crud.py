"""
CRUD operations for schedules.

This module contains all database operations for schedule management,
providing a clean interface between the service layer and the database.

All methods accept an asyncpg.Pool as the first parameter (injected via dependency).
"""

from typing import List, Optional, Dict, Any
import asyncpg
import json
from loguru import logger


class ScheduleCRUD:
    """CRUD operations for schedule management (1 schedule per device)."""

    @staticmethod
    async def upsert(pool: asyncpg.Pool, schedule_data: Dict[str, Any]) -> int:
        """
        Insert or update a schedule for a device.

        Uses INSERT ... ON CONFLICT (device_id) DO UPDATE to ensure
        one schedule per device.

        Returns:
            ID of the created/updated schedule
        """
        async with pool.acquire() as conn:
            result = await conn.fetchval(
                """
                INSERT INTO schedules (
                    device_id, day_schedules, extra_hours, special_days,
                    version, source, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (device_id) DO UPDATE SET
                    day_schedules = EXCLUDED.day_schedules,
                    extra_hours = EXCLUDED.extra_hours,
                    special_days = EXCLUDED.special_days,
                    version = EXCLUDED.version,
                    source = EXCLUDED.source,
                    updated_at = NOW()
                RETURNING id;
                """,
                schedule_data["device_id"],
                schedule_data["day_schedules"],
                schedule_data.get("extra_hours"),
                schedule_data.get("special_days"),
                schedule_data.get("version", "1.0"),
                schedule_data.get("source", "ui"),
            )

            logger.info(f"Schedule upserted for device_id={schedule_data['device_id']} (id={result})")
            return result

    @staticmethod
    async def get_by_device_id(pool: asyncpg.Pool, device_id: int) -> Optional[asyncpg.Record]:
        """
        Get the schedule for a device.

        Args:
            pool: Database connection pool
            device_id: ID of the device

        Returns:
            Schedule record or None if not found
        """
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.device_id = $1;
                """,
                device_id,
            )

    @staticmethod
    async def get_all(pool: asyncpg.Pool) -> List[asyncpg.Record]:
        """
        Get all schedules.

        Returns:
            List of schedule records ordered by device_id
        """
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                ORDER BY s.device_id;
                """
            )

    @staticmethod
    async def get_by_day(pool: asyncpg.Pool, day: str) -> List[asyncpg.Record]:
        """
        Get all schedules that include a specific day.

        Args:
            pool: Database connection pool
            day: Day of the week (monday, tuesday, etc.)

        Returns:
            List of schedules active on the specified day
        """
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT s.id, s.device_id, s.day_schedules, s.extra_hours, s.special_days,
                       s.created_at, s.updated_at, s.version, s.source,
                       d.device_key AS device_name
                FROM schedules s
                LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.day_schedules ? $1
                ORDER BY s.device_id;
                """,
                day,
            )

    @staticmethod
    async def partial_update(
        pool: asyncpg.Pool,
        device_id: int,
        update_data: Dict[str, Any],
    ) -> bool:
        """
        Partially update a schedule by device_id.

        Args:
            pool: Database connection pool
            device_id: ID of the device
            update_data: Dictionary containing fields to update

        Returns:
            True if schedule was updated, False if not found
        """
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM schedules WHERE device_id = $1",
                device_id,
            )
            if not existing:
                return False

            update_fields = []
            values = []
            param_idx = 2  # $1 is device_id

            for field, value in update_data.items():
                update_fields.append(f"{field} = ${param_idx}")
                values.append(value)
                param_idx += 1

            if not update_fields:
                return True

            update_fields.append("updated_at = NOW()")

            query = f"""
                UPDATE schedules
                SET {', '.join(update_fields)}
                WHERE device_id = $1
            """

            await conn.execute(query, device_id, *values)
            logger.info(f"Schedule for device_id={device_id} partially updated")
            return True

    @staticmethod
    async def delete_by_device_id(pool: asyncpg.Pool, device_id: int) -> bool:
        """
        Delete the schedule for a device.

        Args:
            pool: Database connection pool
            device_id: ID of the device

        Returns:
            True if schedule was deleted, False if not found
        """
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM schedules WHERE device_id = $1",
                device_id,
            )
            deleted_count = int(result.split()[-1])
            logger.info(f"Schedule for device_id={device_id} deleted: {deleted_count > 0}")
            return deleted_count > 0

    @staticmethod
    async def get_special_days(pool: asyncpg.Pool, device_id: int) -> Optional[Dict[str, Any]]:
        """
        Get special_days JSONB for a device.

        Returns:
            Dictionary of special days, empty dict if none, None if device not found
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT special_days FROM schedules WHERE device_id = $1",
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
        to match flexibly (e.g. '1103' matches device_key='1103' or
        display_name='CNC 1103').

        Returns:
            Device ID or None if not found
        """
        async with pool.acquire() as conn:
            # Exact match on device_key (most common from frontend)
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

            # Partial match on display_name (e.g. "1103" -> "CNC 1103")
            row = await conn.fetchrow(
                "SELECT id FROM devices WHERE display_name ILIKE '%' || $1 || '%'",
                device_name,
            )
            if row:
                return row["id"]

            return None


schedule_crud = ScheduleCRUD()
