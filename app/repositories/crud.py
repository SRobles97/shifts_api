"""
CRUD operations for schedules.

This module contains all database operations for schedule management,
providing a clean interface between the API endpoints and the database.
"""

from typing import List, Optional, Dict, Any
import asyncpg
import json
from loguru import logger

from ..core.postgres import get_postgres


class ScheduleCRUD:
    """CRUD operations for schedule management."""

    @staticmethod
    async def create_or_update(schedule_data: Dict[str, Any]) -> None:
        """
        Create or update a schedule for a device.

        Args:
            schedule_data: Dictionary containing schedule information

        Raises:
            Exception: If database operation fails
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO schedules (device_name, day_schedules, extra_hours, special_days, version, source, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (device_name) DO UPDATE SET
                    day_schedules      = EXCLUDED.day_schedules,
                    extra_hours        = EXCLUDED.extra_hours,
                    special_days       = EXCLUDED.special_days,
                    version            = EXCLUDED.version,
                    source             = EXCLUDED.source,
                    updated_at         = NOW();
                """,
                schedule_data["device_name"],
                schedule_data["day_schedules"],
                schedule_data.get("extra_hours"),
                schedule_data.get("special_days"),
                schedule_data.get("version", "1.0"),
                schedule_data.get("source", "api"),
            )
            logger.info(
                f"Schedule for device {schedule_data['device_name']} created/updated"
            )

    @staticmethod
    async def get_by_device_name(device_name: str) -> Optional[asyncpg.Record]:
        """
        Get schedule by device name.

        Args:
            device_name: Name of the device

        Returns:
            Schedule record or None if not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days, created_at, updated_at,
                           version, source
                    FROM schedules
                    WHERE device_name = $1;
                    """
            return await conn.fetchrow(query, device_name)

    @staticmethod
    async def get_all() -> List[asyncpg.Record]:
        """
        Get all schedules.

        Returns:
            List of all schedule records
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days, created_at, updated_at,
                           version, source
                    FROM schedules
                    ORDER BY created_at DESC;
                    """
            return await conn.fetch(query)

    @staticmethod
    async def get_by_day(day: str) -> List[asyncpg.Record]:
        """
        Get all schedules that include a specific day.

        Args:
            day: Day of the week (monday, tuesday, etc.)

        Returns:
            List of schedules active on the specified day
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days, created_at, updated_at,
                           version, source
                    FROM schedules
                    WHERE day_schedules ? $1
                    ORDER BY device_name;
                    """
            return await conn.fetch(query, day)

    @staticmethod
    async def partial_update(device_name: str, update_data: Dict[str, Any]) -> bool:
        """
        Partially update a schedule for a device.

        Args:
            device_name: Name of the device
            update_data: Dictionary containing fields to update

        Returns:
            True if schedule was updated, False if not found

        Raises:
            Exception: If database operation fails
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            # First check if the schedule exists
            existing = await conn.fetchrow(
                "SELECT id FROM schedules WHERE device_name = $1", device_name
            )
            if not existing:
                return False

            # Build dynamic update query
            update_fields = []
            values = []
            param_idx = 2  # $1 is for device_name

            for field, value in update_data.items():
                if value is not None:
                    update_fields.append(f"{field} = ${param_idx}")
                    values.append(value)
                    param_idx += 1

            if not update_fields:
                return True  # No updates needed

            # Always update the updated_at field
            update_fields.append("updated_at = NOW()")

            query = f"""
                UPDATE schedules
                SET {', '.join(update_fields)}
                WHERE device_name = $1
            """

            await conn.execute(query, device_name, *values)
            logger.info(f"Schedule for device {device_name} partially updated")
            return True

    @staticmethod
    async def get_schedules_for_date(target_date: str) -> List[asyncpg.Record]:
        """
        Get all schedules that are active on a specific date.

        Considers:
        - Special days with exact date match
        - Recurring special days (yearly)
        - Regular weekday schedules

        Args:
            target_date: ISO date string (YYYY-MM-DD)

        Returns:
            List of schedule records active on the date
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            # Parse date to extract weekday and month-day
            from datetime import datetime
            date_obj = datetime.strptime(target_date, "%Y-%m-%d")
            weekday = date_obj.strftime("%A").lower()
            month_day = date_obj.strftime("-%m-%d")  # Format: -MM-DD for matching

            query = """
                SELECT id, device_name, day_schedules, extra_hours, special_days,
                       created_at, updated_at, version, source
                FROM schedules
                WHERE
                    -- Has special day for exact date
                    (special_days ? $1)
                    OR
                    -- Has recurring special day (check for any key ending with month-day)
                    (EXISTS (
                        SELECT 1
                        FROM jsonb_object_keys(special_days) AS key
                        WHERE key LIKE '%' || $2
                        AND (special_days->key->>'isRecurring')::boolean = true
                    ))
                    OR
                    -- Regular weekday schedule
                    (day_schedules ? $3)
                ORDER BY device_name;
            """

            return await conn.fetch(query, target_date, month_day, weekday)

    @staticmethod
    async def get_special_days_in_range(
        device_name: str,
        start_date: str,
        end_date: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get special days for a device within a date range.

        Args:
            device_name: Name of the device
            start_date: ISO date string (YYYY-MM-DD)
            end_date: ISO date string (YYYY-MM-DD)

        Returns:
            Dictionary of special days in range, or None if device not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                SELECT special_days
                FROM schedules
                WHERE device_name = $1;
            """
            result = await conn.fetchval(query, device_name)

            if result is None:
                return None

            # Parse and filter by date range
            all_special_days = result if isinstance(result, dict) else json.loads(result)
            if not all_special_days:
                return {}

            filtered = {}
            for date_str, special_day in all_special_days.items():
                if start_date <= date_str <= end_date:
                    filtered[date_str] = special_day

            return filtered

    @staticmethod
    async def delete_by_device_name(device_name: str) -> bool:
        """
        Delete schedule by device name.

        Args:
            device_name: Name of the device

        Returns:
            True if schedule was deleted, False if not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM schedules WHERE device_name = $1", device_name
            )
            deleted_count = int(result.split()[-1])
            logger.info(
                f"Schedule for device {device_name} deleted: {deleted_count > 0}"
            )
            return deleted_count > 0


# Instance for easy importing
schedule_crud = ScheduleCRUD()
