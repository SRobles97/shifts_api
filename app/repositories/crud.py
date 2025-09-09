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
                INSERT INTO schedules (device_name, active_days, work_start_time, work_end_time,
                                     break_start_time, break_duration, extra_hours, version, source, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (device_name) DO UPDATE SET 
                    active_days        = EXCLUDED.active_days,
                    work_start_time    = EXCLUDED.work_start_time,
                    work_end_time      = EXCLUDED.work_end_time,
                    break_start_time   = EXCLUDED.break_start_time,
                    break_duration     = EXCLUDED.break_duration,
                    extra_hours        = EXCLUDED.extra_hours,
                    version            = EXCLUDED.version,
                    source             = EXCLUDED.source,
                    updated_at         = NOW();
                """,
                schedule_data["device_name"],
                schedule_data["active_days"],
                schedule_data["work_start_time"],
                schedule_data["work_end_time"],
                schedule_data["break_start_time"],
                schedule_data["break_duration"],
                schedule_data.get("extra_hours"),
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
                    SELECT id, device_name, active_days, work_start_time, work_end_time,
                           break_start_time, break_duration, extra_hours, created_at, updated_at,
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
                    SELECT id, device_name, active_days, work_start_time, work_end_time,
                           break_start_time, break_duration, extra_hours, created_at, updated_at,
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
                    SELECT id, device_name, active_days, work_start_time, work_end_time,
                           break_start_time, break_duration, extra_hours, created_at, updated_at,
                           version, source
                    FROM schedules
                    WHERE $1 = ANY(active_days)
                    ORDER BY device_name;
                    """
            return await conn.fetch(query, day)

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
