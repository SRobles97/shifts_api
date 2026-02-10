"""
Service layer for schedule business logic.

Orchestrates between schemas, models, and the CRUD repository.
Raises ValueError for validation errors and LookupError for not-found.
The router maps these to HTTP status codes.
"""

import json
import re
from datetime import datetime, time
from typing import Any, Dict, List, Optional

import asyncpg

from ..models.schedule import (
    Break,
    DaySchedule,
    ExtraHour,
    Schedule,
    ScheduleEntity,
    SpecialDay,
    WorkHours,
)
from ..repositories.crud import schedule_crud
from ..schemas.schedule import (
    AllScheduleStatsResponse,
    BreakSchema,
    DayScheduleSchema,
    ExtraHourSchema,
    MetadataSchema,
    ScheduleCreate,
    ScheduleDeleteResponse,
    SchedulePatch,
    ScheduleRead,
    ScheduleStatsSchema,
    ScheduleUpdate,
    SingleScheduleStatsResponse,
    SpecialDaySchema,
    WorkHoursSchema,
)

VALID_DAYS = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_time_string(time_str: str) -> time:
    return datetime.strptime(time_str, "%H:%M").time()


def _time_to_minutes(time_obj: time) -> int:
    return time_obj.hour * 60 + time_obj.minute


def _serialize_day_schedules(schedule_dict: Dict[str, DayScheduleSchema]) -> str:
    """Convert DayScheduleSchema dict to JSON string for DB storage."""
    result = {}
    for day, ds in schedule_dict.items():
        result[day] = {
            "workHours": {"start": ds.work_hours.start, "end": ds.work_hours.end},
            "break": {
                "start": ds.break_time.start,
                "durationMinutes": ds.break_time.duration_minutes,
            },
        }
    return json.dumps(result)


def _serialize_extra_hours(
    extra_hours: Optional[Dict[str, List[ExtraHourSchema]]],
) -> Optional[str]:
    if not extra_hours:
        return None
    return json.dumps(
        {day: [h.model_dump() for h in hours] for day, hours in extra_hours.items()}
    )


def _serialize_special_days(
    special_days: Optional[Dict[str, SpecialDaySchema]],
) -> Optional[str]:
    if not special_days:
        return None
    return json.dumps(
        {date_str: sd.model_dump(by_alias=True) for date_str, sd in special_days.items()}
    )


def _build_schedule_read(db_record: dict) -> ScheduleRead:
    """Build a ScheduleRead from a database record."""
    # Parse day_schedules
    day_schedules_data = db_record["day_schedules"]
    if isinstance(day_schedules_data, str):
        day_schedules_data = json.loads(day_schedules_data)

    day_schedules_dict = {}
    for day, cfg in day_schedules_data.items():
        day_schedules_dict[day] = DayScheduleSchema(
            work_hours=WorkHoursSchema(**cfg["workHours"]),
            break_time=BreakSchema(**cfg["break"]),
        )

    # Parse extra_hours
    extra_hours_dict = None
    if db_record["extra_hours"]:
        eh_data = db_record["extra_hours"]
        if isinstance(eh_data, str):
            eh_data = json.loads(eh_data)
        extra_hours_dict = {
            day: [ExtraHourSchema(**h) for h in hours]
            for day, hours in eh_data.items()
        }

    # Parse special_days
    special_days_dict = None
    if db_record.get("special_days"):
        sd_data = db_record["special_days"]
        if isinstance(sd_data, str):
            sd_data = json.loads(sd_data)
        special_days_dict = {}
        for date_str, sd in sd_data.items():
            work_hours = WorkHoursSchema(**sd["workHours"]) if sd.get("workHours") else None
            break_time = BreakSchema(**sd["break"]) if sd.get("break") else None
            special_days_dict[date_str] = SpecialDaySchema(
                name=sd["name"],
                type=sd["type"],
                work_hours=work_hours,
                break_time=break_time,
                is_recurring=sd.get("isRecurring", False),
                recurrence_pattern=sd.get("recurrencePattern"),
            )

    return ScheduleRead(
        id=str(db_record["id"]),
        device_id=db_record["device_id"],
        device_name=db_record.get("device_name"),
        schedule=day_schedules_dict,
        extra_hours=extra_hours_dict,
        special_days=special_days_dict,
        metadata=MetadataSchema(
            created_at=db_record["created_at"],
            version=db_record["version"],
            source=db_record["source"],
        ),
    )


def _calculate_work_hours_usage(db_schedule: dict, current_time: datetime) -> dict:
    """Calculate work hours usage statistics for a schedule."""
    current_day = current_time.strftime("%A").lower()
    current_time_obj = current_time.time()
    current_time_str = current_time_obj.strftime("%H:%M")

    day_schedules_data = db_schedule["day_schedules"]
    if isinstance(day_schedules_data, str):
        day_schedules_data = json.loads(day_schedules_data)

    if current_day not in day_schedules_data:
        return {
            "device_id": db_schedule["device_id"],
            "schedule_start": "00:00",
            "schedule_end": "00:00",
            "current_time": current_time_str,
            "hours_used": 0.0,
            "total_work_hours": 0.0,
            "usage_percentage": 0.0,
        }

    today_schedule = day_schedules_data[current_day]
    work_start = _parse_time_string(today_schedule["workHours"]["start"])
    work_end = _parse_time_string(today_schedule["workHours"]["end"])
    break_start = _parse_time_string(today_schedule["break"]["start"])
    break_duration = today_schedule["break"]["durationMinutes"]

    work_start_minutes = _time_to_minutes(work_start)
    work_end_minutes = _time_to_minutes(work_end)
    total_work_minutes = work_end_minutes - work_start_minutes - break_duration
    total_work_hours = total_work_minutes / 60.0

    current_minutes = _time_to_minutes(current_time_obj)

    if current_minutes < work_start_minutes:
        hours_used = 0.0
        usage_percentage = 0.0
    elif current_minutes >= work_end_minutes:
        hours_used = total_work_hours
        usage_percentage = 100.0
    else:
        work_minutes_elapsed = current_minutes - work_start_minutes
        break_start_minutes = _time_to_minutes(break_start)
        if current_minutes > break_start_minutes + break_duration:
            work_minutes_elapsed -= break_duration
        elif current_minutes > break_start_minutes:
            work_minutes_elapsed -= current_minutes - break_start_minutes

        hours_used = max(0, work_minutes_elapsed) / 60.0
        usage_percentage = (
            (hours_used / total_work_hours * 100) if total_work_hours > 0 else 0.0
        )

    usage_percentage = min(100.0, max(0.0, usage_percentage))

    return {
        "device_id": db_schedule["device_id"],
        "schedule_start": work_start.strftime("%H:%M"),
        "schedule_end": work_end.strftime("%H:%M"),
        "current_time": current_time_str,
        "hours_used": round(hours_used, 2),
        "total_work_hours": round(total_work_hours, 2),
        "usage_percentage": round(usage_percentage, 2),
    }


def _parse_break(data: dict) -> Break:
    """Parse a break dict from JSONB (camelCase) into a Break model (snake_case)."""
    return Break(
        start=data["start"],
        duration_minutes=data.get("durationMinutes", data.get("duration_minutes")),
    )


def _db_record_to_entity(db_record: dict) -> ScheduleEntity:
    """Convert a DB record into a ScheduleEntity model instance."""
    day_schedules_data = db_record["day_schedules"]
    if isinstance(day_schedules_data, str):
        day_schedules_data = json.loads(day_schedules_data)

    day_schedules_dict = {}
    for day, cfg in day_schedules_data.items():
        day_schedules_dict[day] = DaySchedule(
            work_hours=WorkHours(**cfg["workHours"]),
            break_time=_parse_break(cfg["break"]),
        )

    schedule = Schedule(day_schedules=day_schedules_dict)

    extra_hours_dict = None
    if db_record.get("extra_hours"):
        eh_data = db_record["extra_hours"]
        if isinstance(eh_data, str):
            eh_data = json.loads(eh_data)
        extra_hours_dict = {
            day: [ExtraHour(**h) for h in hours] for day, hours in eh_data.items()
        }

    special_days_dict = None
    if db_record.get("special_days"):
        sd_data = db_record["special_days"]
        if isinstance(sd_data, str):
            sd_data = json.loads(sd_data)
        special_days_dict = {
            date_str: SpecialDay(
                name=sd["name"],
                type=sd["type"],
                work_hours=WorkHours(**sd["workHours"]) if sd.get("workHours") else None,
                break_time=_parse_break(sd["break"]) if sd.get("break") else None,
                is_recurring=sd.get("isRecurring", False),
                recurrence_pattern=sd.get("recurrencePattern"),
            )
            for date_str, sd in sd_data.items()
        }

    return ScheduleEntity(
        id=db_record["id"],
        device_id=db_record["device_id"],
        schedule=schedule,
        extra_hours=extra_hours_dict,
        special_days=special_days_dict,
        created_at=db_record["created_at"],
        updated_at=db_record["updated_at"],
        version=db_record["version"],
        source=db_record["source"],
    )


class ScheduleService:
    """Service layer encapsulating schedule business logic."""

    @staticmethod
    async def _resolve_device_id(pool: asyncpg.Pool, data: ScheduleCreate) -> int:
        """Resolve device_id from either deviceId or deviceName."""
        if data.device_id:
            return data.device_id

        if data.device_name:
            device_id = await schedule_crud.get_device_id_by_name(pool, data.device_name)
            if not device_id:
                raise LookupError(
                    f"Device with name '{data.device_name}' not found"
                )
            return device_id

        raise ValueError("Either deviceId or deviceName must be provided")

    @staticmethod
    async def create_schedule(pool: asyncpg.Pool, data: ScheduleCreate) -> ScheduleRead:
        device_id = await ScheduleService._resolve_device_id(pool, data)

        schedule_data = {
            "device_id": device_id,
            "day_schedules": _serialize_day_schedules(data.schedule),
            "extra_hours": _serialize_extra_hours(data.extra_hours),
            "special_days": _serialize_special_days(data.special_days),
            "version": data.metadata.version if data.metadata else "1.0",
            "source": data.metadata.source if data.metadata else "ui",
        }

        schedule_id = await schedule_crud.upsert(pool, schedule_data)

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            raise RuntimeError("Failed to retrieve created schedule")

        return _build_schedule_read(db_record)

    @staticmethod
    async def update_schedule(
        pool: asyncpg.Pool, device_id: int, data: ScheduleUpdate
    ) -> ScheduleRead:
        existing = await schedule_crud.get_by_device_id(pool, device_id)
        if not existing:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        schedule_data = {
            "device_id": device_id,
            "day_schedules": _serialize_day_schedules(data.schedule),
            "extra_hours": _serialize_extra_hours(data.extra_hours),
            "special_days": _serialize_special_days(data.special_days),
            "version": data.metadata.version if data.metadata else "1.0",
            "source": data.metadata.source if data.metadata else "ui",
        }

        await schedule_crud.upsert(pool, schedule_data)

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        return _build_schedule_read(db_record)

    @staticmethod
    async def patch_schedule(
        pool: asyncpg.Pool, device_id: int, data: SchedulePatch
    ) -> ScheduleRead:
        existing = await schedule_crud.get_by_device_id(pool, device_id)
        if not existing:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        update_data: Dict[str, Any] = {}

        if data.schedule is not None:
            update_data["day_schedules"] = _serialize_day_schedules(data.schedule)

        if data.extra_hours is not None:
            update_data["extra_hours"] = _serialize_extra_hours(data.extra_hours)

        if data.special_days is not None:
            update_data["special_days"] = _serialize_special_days(data.special_days)

        if data.metadata:
            if data.metadata.version:
                update_data["version"] = data.metadata.version
            if data.metadata.source:
                update_data["source"] = data.metadata.source

        if update_data:
            await schedule_crud.partial_update(pool, device_id, update_data)

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        return _build_schedule_read(db_record)

    @staticmethod
    async def get_schedule(pool: asyncpg.Pool, device_id: int) -> Optional[ScheduleRead]:
        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            return None
        return _build_schedule_read(db_record)

    @staticmethod
    async def get_all_schedules(pool: asyncpg.Pool) -> List[ScheduleRead]:
        db_records = await schedule_crud.get_all(pool)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def get_schedules_by_day(pool: asyncpg.Pool, day: str) -> List[ScheduleRead]:
        day_lower = day.lower()
        if day_lower not in VALID_DAYS:
            raise ValueError("Día inválido. Use: monday, tuesday, etc.")
        db_records = await schedule_crud.get_by_day(pool, day_lower)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def delete_schedule(pool: asyncpg.Pool, device_id: int) -> bool:
        deleted = await schedule_crud.delete_by_device_id(pool, device_id)
        if not deleted:
            raise LookupError(f"Schedule for device_id={device_id} not found")
        return True

    @staticmethod
    async def get_all_stats(pool: asyncpg.Pool) -> AllScheduleStatsResponse:
        current_time = datetime.now()
        db_records = await schedule_crud.get_all(pool)

        device_stats = []
        for rec in db_records:
            stats = _calculate_work_hours_usage(rec, current_time)
            device_stats.append(ScheduleStatsSchema(**stats))

        return AllScheduleStatsResponse(
            request_time=current_time.strftime("%H:%M"),
            devices=device_stats,
        )

    @staticmethod
    async def get_device_stats(
        pool: asyncpg.Pool, device_id: int
    ) -> SingleScheduleStatsResponse:
        current_time = datetime.now()
        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        stats = _calculate_work_hours_usage(db_record, current_time)
        device_stats = ScheduleStatsSchema(**stats)

        return SingleScheduleStatsResponse(
            request_time=current_time.strftime("%H:%M"),
            device_stats=device_stats,
        )

    @staticmethod
    async def get_special_days(pool: asyncpg.Pool, device_id: int) -> Dict[str, Any]:
        result = await schedule_crud.get_special_days(pool, device_id)
        if result is None:
            raise LookupError(f"Schedule for device_id={device_id} not found")
        return result

    @staticmethod
    async def add_special_day(
        pool: asyncpg.Pool,
        device_id: int,
        date_str: str,
        special_day: SpecialDaySchema,
    ) -> ScheduleRead:
        if not _DATE_RE.match(date_str):
            raise ValueError("Formato de fecha inválido. Use YYYY-MM-DD")

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        sd_data = db_record.get("special_days")
        if sd_data:
            special_days = json.loads(sd_data) if isinstance(sd_data, str) else sd_data
        else:
            special_days = {}

        special_days[date_str] = special_day.model_dump(by_alias=True)

        await schedule_crud.partial_update(
            pool, device_id, {"special_days": json.dumps(special_days)}
        )

        updated = await schedule_crud.get_by_device_id(pool, device_id)
        return _build_schedule_read(updated)

    @staticmethod
    async def delete_special_day(
        pool: asyncpg.Pool, device_id: int, date_str: str
    ) -> ScheduleDeleteResponse:
        if not _DATE_RE.match(date_str):
            raise ValueError("Formato de fecha inválido. Use YYYY-MM-DD")

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        sd_data = db_record.get("special_days")
        if not sd_data:
            raise LookupError("No hay días especiales para este dispositivo")

        special_days = json.loads(sd_data) if isinstance(sd_data, str) else sd_data
        if date_str not in special_days:
            raise LookupError("Día especial no encontrado")

        del special_days[date_str]

        await schedule_crud.partial_update(
            pool,
            device_id,
            {"special_days": json.dumps(special_days) if special_days else None},
        )

        return ScheduleDeleteResponse(
            message=f"Día especial {date_str} eliminado para device_id={device_id}"
        )

    @staticmethod
    async def get_effective_schedule(
        pool: asyncpg.Pool, device_id: int, date_str: str
    ) -> Optional[DayScheduleSchema]:
        if not _DATE_RE.match(date_str):
            raise ValueError("Formato de fecha inválido. Use YYYY-MM-DD")

        db_record = await schedule_crud.get_by_device_id(pool, device_id)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        entity = _db_record_to_entity(db_record)
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        effective = entity.get_effective_schedule_for_date(target_date)

        if effective is None:
            return None

        return DayScheduleSchema(
            work_hours=WorkHoursSchema(
                start=effective.work_hours.start,
                end=effective.work_hours.end,
            ),
            break_time=BreakSchema(
                start=effective.break_time.start,
                duration_minutes=effective.break_time.duration_minutes,
            ),
        )


schedule_service = ScheduleService()
