"""
Read-only lookups against the external `devices` table.

`devices` is owned by another service; shifts_api only ever reads it. The
notification payload needs the company and a human-facing name, neither of
which lives on `device_schedules`.
"""

from typing import Optional

import asyncpg

from ..models.notification_event import DeviceRef


class DeviceCRUD:
    """Device lookups needed to describe a schedule change."""

    @staticmethod
    async def get_device_ref(pool: asyncpg.Pool, device_id: int) -> Optional[DeviceRef]:
        """Resolve the company and naming for a device, or None if it is gone."""
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, company_id, device_key, display_name
                FROM devices
                WHERE id = $1;
                """,
                device_id,
            )
        if row is None:
            return None
        return DeviceRef(
            id=row["id"],
            company_id=row["company_id"],
            key=row["device_key"] or "",
            display_name=row["display_name"] or "",
        )


device_crud = DeviceCRUD()
