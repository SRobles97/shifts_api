"""
CRUD operations for schedules.

This module contains all database operations for schedule management,
providing a clean interface between the API endpoints and the database.
"""

from typing import List, Optional, Dict, Any
import asyncpg
import json
from loguru import logger
from datetime import date

from ..core.postgres import get_postgres


class ScheduleCRUD:
    """CRUD operations for schedule management."""

    @staticmethod
    async def create_or_update(schedule_data: Dict[str, Any]) -> int:
        """
        Create a new schedule for a device with date range support.

        Now creates a new schedule entry instead of updating existing ones.
        Returns the ID of the created schedule.

        Validation:
        - Checks for overlapping date ranges
        - Prevents creating schedules with invalid date ranges

        Args:
            schedule_data: Dictionary containing schedule information including start_date and end_date

        Returns:
            ID of the created schedule

        Raises:
            ValueError: If date range overlaps with existing schedule
            Exception: If database operation fails
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            device_name = schedule_data["device_name"]
            start_date = schedule_data.get("start_date", date.today())
            end_date = schedule_data.get("end_date")

            # Check for overlapping date ranges
            overlap_check = """
                SELECT id, start_date, end_date
                FROM schedules
                WHERE device_name = $1
                AND daterange($2::date, COALESCE($3::date, '9999-12-31'::date), '[]') &&
                    daterange(start_date, COALESCE(end_date, '9999-12-31'::date), '[]')
            """
            overlapping = await conn.fetch(overlap_check, device_name, start_date, end_date)

            if overlapping:
                # Format error message with conflicting ranges
                conflicts = [f"{r['start_date']} to {r['end_date'] or 'indefinite'}" for r in overlapping]
                raise ValueError(
                    f"Date range overlaps with existing schedule(s): {', '.join(conflicts)}"
                )

            # Insert new schedule
            result = await conn.fetchval(
                """
                INSERT INTO schedules (
                    device_name, day_schedules, extra_hours, special_days,
                    start_date, end_date, version, source, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                RETURNING id;
                """,
                device_name,
                schedule_data["day_schedules"],
                schedule_data.get("extra_hours"),
                schedule_data.get("special_days"),
                start_date,
                end_date,
                schedule_data.get("version", "1.0"),
                schedule_data.get("source", "api")
            )

            logger.info(
                f"Schedule created for device {device_name} "
                f"(id={result}, range: {start_date} to {end_date or 'indefinite'})"
            )
            return result

    @staticmethod
    async def get_by_device_name(device_name: str) -> Optional[asyncpg.Record]:
        """
        Get currently active schedule for a device.

        Returns the schedule that is active today (start_date <= today <= end_date).
        If multiple schedules could match (shouldn't happen with overlap constraint),
        returns the one with the latest start_date.

        Args:
            device_name: Name of the device

        Returns:
            Currently active schedule record or None if not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                SELECT id, device_name, day_schedules, extra_hours, special_days,
                       start_date, end_date, created_at, updated_at, version, source
                FROM schedules
                WHERE device_name = $1
                AND start_date <= CURRENT_DATE
                AND (end_date IS NULL OR end_date >= CURRENT_DATE)
                ORDER BY start_date DESC
                LIMIT 1;
            """
            return await conn.fetchrow(query, device_name)

    @staticmethod
    async def get_all(include_inactive: bool = False) -> List[asyncpg.Record]:
        """
        Get all schedules, with option to filter by active status.

        Args:
            include_inactive: If False, only returns currently active schedules (one per device)
                            If True, returns all schedules including past ones

        Returns:
            List of schedule records
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            if include_inactive:
                query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days,
                           start_date, end_date, created_at, updated_at, version, source
                    FROM schedules
                    ORDER BY device_name, start_date DESC;
                """
            else:
                query = """
                    SELECT DISTINCT ON (device_name)
                           id, device_name, day_schedules, extra_hours, special_days,
                           start_date, end_date, created_at, updated_at, version, source
                    FROM schedules
                    WHERE start_date <= CURRENT_DATE
                    AND (end_date IS NULL OR end_date >= CURRENT_DATE)
                    ORDER BY device_name, start_date DESC;
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
    async def partial_update(
        device_name: str,
        schedule_id: int,
        update_data: Dict[str, Any]
    ) -> bool:
        """
        Partially update a specific schedule.

        Args:
            device_name: Name of the device
            schedule_id: ID of the schedule to update
            update_data: Dictionary containing fields to update

        Returns:
            True if schedule was updated, False if not found

        Raises:
            ValueError: If date range update would cause overlap
            Exception: If database operation fails
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            # Check if schedule exists
            existing = await conn.fetchrow(
                "SELECT id, start_date, end_date FROM schedules WHERE device_name = $1 AND id = $2",
                device_name, schedule_id
            )
            if not existing:
                return False

            # If updating dates, check for overlaps
            if "start_date" in update_data or "end_date" in update_data:
                new_start = update_data.get("start_date", existing["start_date"])
                new_end = update_data.get("end_date", existing["end_date"])

                # Check overlap with other schedules (excluding this one)
                overlap_check = """
                    SELECT id, start_date, end_date
                    FROM schedules
                    WHERE device_name = $1 AND id != $2
                    AND daterange($3::date, COALESCE($4::date, '9999-12-31'::date), '[]') &&
                        daterange(start_date, COALESCE(end_date, '9999-12-31'::date), '[]')
                """
                overlapping = await conn.fetch(overlap_check, device_name, schedule_id, new_start, new_end)

                if overlapping:
                    conflicts = [f"{r['start_date']} to {r['end_date'] or 'indefinite'}" for r in overlapping]
                    raise ValueError(
                        f"Updated date range overlaps with existing schedule(s): {', '.join(conflicts)}"
                    )

            # Build dynamic update query
            update_fields = []
            values = []
            param_idx = 3  # $1 is device_name, $2 is schedule_id

            for field, value in update_data.items():
                # Include all fields, even if value is None (to support setting to NULL)
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
                WHERE device_name = $1 AND id = $2
            """

            await conn.execute(query, device_name, schedule_id, *values)
            logger.info(f"Schedule {schedule_id} for device {device_name} partially updated")
            return True

    @staticmethod
    async def get_by_device_name_and_id(device_name: str, schedule_id: int) -> Optional[asyncpg.Record]:
        """
        Get specific schedule by device name and schedule ID.

        Args:
            device_name: Name of the device
            schedule_id: ID of the schedule

        Returns:
            Schedule record or None if not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                SELECT id, device_name, day_schedules, extra_hours, special_days,
                       start_date, end_date, created_at, updated_at, version, source
                FROM schedules
                WHERE device_name = $1 AND id = $2;
            """
            return await conn.fetchrow(query, device_name, schedule_id)

    @staticmethod
    async def get_all_by_device_name(device_name: str, include_past: bool = False) -> List[asyncpg.Record]:
        """
        Get all schedules for a device, ordered by start_date.

        Args:
            device_name: Name of the device
            include_past: Whether to include schedules that have ended

        Returns:
            List of schedule records ordered by start_date DESC (newest first)
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            if include_past:
                query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days,
                           start_date, end_date, created_at, updated_at, version, source
                    FROM schedules
                    WHERE device_name = $1
                    ORDER BY start_date DESC;
                """
            else:
                query = """
                    SELECT id, device_name, day_schedules, extra_hours, special_days,
                           start_date, end_date, created_at, updated_at, version, source
                    FROM schedules
                    WHERE device_name = $1
                    AND (end_date IS NULL OR end_date >= CURRENT_DATE)
                    ORDER BY start_date DESC;
                """
            return await conn.fetch(query, device_name)

    @staticmethod
    async def get_schedule_for_device_on_date(
        device_name: str,
        target_date: date
    ) -> Optional[asyncpg.Record]:
        """
        Get the schedule active for a device on a specific date.

        Args:
            device_name: Name of the device
            target_date: Date to check

        Returns:
            Schedule record active on that date or None
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            query = """
                SELECT id, device_name, day_schedules, extra_hours, special_days,
                       start_date, end_date, created_at, updated_at, version, source
                FROM schedules
                WHERE device_name = $1
                AND start_date <= $2
                AND (end_date IS NULL OR end_date >= $2)
                ORDER BY start_date DESC
                LIMIT 1;
            """
            return await conn.fetchrow(query, device_name, target_date)

    @staticmethod
    async def get_schedules_for_date(target_date: str) -> List[asyncpg.Record]:
        """
        Get all schedules that are active on a specific date.

        Considers:
        - Date range (start_date and end_date)
        - Special days with exact date match
        - Recurring special days (yearly)
        - Regular weekday schedules

        Args:
            target_date: ISO date string (YYYY-MM-DD)

        Returns:
            List of schedule records active on the date (one per device)
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            # Parse date to extract weekday and month-day
            from datetime import datetime
            date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
            weekday = date_obj.strftime("%A").lower()
            month_day = date_obj.strftime("-%m-%d")  # Format: -MM-DD for matching

            query = """
                SELECT DISTINCT ON (device_name)
                       id, device_name, day_schedules, extra_hours, special_days,
                       start_date, end_date, created_at, updated_at, version, source
                FROM schedules
                WHERE start_date <= $1::date
                AND (end_date IS NULL OR end_date >= $1::date)
                AND (
                    -- Has special day for exact date
                    (special_days ? $1)
                    OR
                    -- Has recurring special day
                    (EXISTS (
                        SELECT 1
                        FROM jsonb_object_keys(special_days) AS key
                        WHERE key LIKE '%' || $2
                        AND (special_days->key->>'isRecurring')::boolean = true
                    ))
                    OR
                    -- Regular weekday schedule
                    (day_schedules ? $3)
                )
                ORDER BY device_name, start_date DESC;
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
    async def delete_by_device_name(device_name: str, schedule_id: Optional[int] = None) -> bool:
        """
        Delete schedule(s) by device name.

        Args:
            device_name: Name of the device
            schedule_id: Optional specific schedule ID to delete.
                       If None, deletes ALL schedules for the device.

        Returns:
            True if schedule(s) were deleted, False if not found
        """
        pool = await get_postgres()
        async with pool.acquire() as conn:
            if schedule_id:
                result = await conn.execute(
                    "DELETE FROM schedules WHERE device_name = $1 AND id = $2",
                    device_name, schedule_id
                )
                msg = f"Schedule {schedule_id} for device {device_name}"
            else:
                result = await conn.execute(
                    "DELETE FROM schedules WHERE device_name = $1",
                    device_name
                )
                msg = f"All schedules for device {device_name}"

            deleted_count = int(result.split()[-1])
            logger.info(f"{msg} deleted: {deleted_count > 0}")
            return deleted_count > 0


# Instance for easy importing
schedule_crud = ScheduleCRUD()
