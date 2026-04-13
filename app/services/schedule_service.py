"""
Service layer for schedule business logic.

Orchestrates between schemas, models, and the CRUD repository.
Raises ValueError for validation errors and LookupError for not-found.
The router maps these to HTTP status codes.
"""

import json
import re
from datetime import date, datetime, time
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
_INVALID_DATE_FMT = "Formato de fecha inválido. Use YYYY-MM-DD"


def _parse_time_string(time_str: str) -> time:
    return datetime.strptime(time_str, "%H:%M").time()


def _time_to_minutes(time_obj: time) -> int:
    return time_obj.hour * 60 + time_obj.minute


def _serialize_day_schedules(schedule_dict: Dict[str, DayScheduleSchema]) -> str:
    """Convert DayScheduleSchema dict to JSON string for DB storage."""
    result = {}
    for day, ds in schedule_dict.items():
        day_data: Dict[str, Any] = {
            "workHours": {"start": ds.work_hours.start, "end": ds.work_hours.end},
        }
        if ds.breaks:
            day_data["breaks"] = [
                {"start": b.start, "durationMinutes": b.duration_minutes}
                for b in ds.breaks
            ]
        result[day] = day_data
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


def _load_jsonb(data) -> dict:
    """Parse a JSONB value that may be a string or already a dict."""
    return json.loads(data) if isinstance(data, str) else data


def _parse_breaks_schema(cfg: dict) -> Optional[List[BreakSchema]]:
    """Parse breaks from a day config dict, handling legacy single-object format."""
    if cfg.get("breaks"):
        return [BreakSchema(**b) for b in cfg["breaks"]]
    if cfg.get("break"):
        return [BreakSchema(**cfg["break"])]
    return None


def _parse_breaks_model(cfg: dict) -> Optional[List[Break]]:
    """Parse breaks from a day config dict into Break model instances."""
    if cfg.get("breaks"):
        return [_parse_break(b) for b in cfg["breaks"]]
    if cfg.get("break"):
        return [_parse_break(cfg["break"])]
    return None


def _parse_special_days_schema(sd_data: dict) -> Dict[str, SpecialDaySchema]:
    """Parse special days JSONB into SpecialDaySchema instances."""
    result = {}
    for date_str, sd in sd_data.items():
        work_hours = WorkHoursSchema(**sd["workHours"]) if sd.get("workHours") else None
        result[date_str] = SpecialDaySchema(
            name=sd["name"],
            type=sd["type"],
            work_hours=work_hours,
            breaks=_parse_breaks_schema(sd),
            is_recurring=sd.get("isRecurring", False),
            recurrence_pattern=sd.get("recurrencePattern"),
        )
    return result


def _parse_special_days_model(sd_data: dict) -> Dict[str, SpecialDay]:
    """Parse special days JSONB into SpecialDay model instances."""
    result = {}
    for date_str, sd in sd_data.items():
        result[date_str] = SpecialDay(
            name=sd["name"],
            type=sd["type"],
            work_hours=WorkHours(**sd["workHours"]) if sd.get("workHours") else None,
            breaks=_parse_breaks_model(sd),
            is_recurring=sd.get("isRecurring", False),
            recurrence_pattern=sd.get("recurrencePattern"),
        )
    return result


def _build_schedule_read(db_record: dict) -> ScheduleRead:
    """Build a ScheduleRead from a database record."""
    day_schedules_data = _load_jsonb(db_record["day_schedules"])
    day_schedules_dict = {
        day: DayScheduleSchema(
            work_hours=WorkHoursSchema(**cfg["workHours"]),
            breaks=_parse_breaks_schema(cfg),
        )
        for day, cfg in day_schedules_data.items()
    }

    extra_hours_dict = None
    if db_record["extra_hours"]:
        eh_data = _load_jsonb(db_record["extra_hours"])
        extra_hours_dict = {
            day: [ExtraHourSchema(**h) for h in hours]
            for day, hours in eh_data.items()
        }

    special_days_dict = None
    if db_record.get("special_days"):
        special_days_dict = _parse_special_days_schema(
            _load_jsonb(db_record["special_days"])
        )

    return ScheduleRead(
        id=str(db_record["id"]),
        device_id=db_record["device_id"],
        device_name=db_record.get("device_name"),
        shift_type=db_record.get("shift_type", "day"),
        schedule=day_schedules_dict,
        extra_hours=extra_hours_dict,
        special_days=special_days_dict,
        valid_from=db_record["valid_from"],
        valid_to=db_record.get("valid_to"),
        metadata=MetadataSchema(
            created_at=db_record["created_at"],
            version=db_record["version"],
            source=db_record["source"],
        ),
    )


def _normalize_minute(m: int, work_start_min: int, crosses_midnight: bool) -> int:
    """Normalize a minute value for cross-midnight comparison."""
    if crosses_midnight and m < work_start_min:
        return m + 24 * 60
    return m


def _parse_breaks_data(today_schedule: dict) -> list:
    """Extract breaks from a day schedule, handling array and legacy single-object formats."""
    if today_schedule.get("breaks"):
        return today_schedule["breaks"]
    if today_schedule.get("break"):
        return [today_schedule["break"]]
    return []


def _deduct_breaks(
    breaks_data: list,
    current_norm: int,
    work_start_minutes: int,
    crosses_midnight: bool,
) -> int:
    """Calculate total break minutes to deduct from elapsed work time."""
    deduction = 0
    for b in breaks_data:
        b_start = _normalize_minute(
            _time_to_minutes(_parse_time_string(b["start"])),
            work_start_minutes,
            crosses_midnight,
        )
        b_dur = b["durationMinutes"]
        if current_norm > b_start + b_dur:
            deduction += b_dur
        elif current_norm > b_start:
            deduction += current_norm - b_start
    return deduction


def _calculate_work_hours_usage(db_schedule: dict, current_time: datetime) -> dict:
    """Calculate work hours usage statistics for a schedule (handles cross-midnight)."""
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

    breaks_data = _parse_breaks_data(today_schedule)

    total_break_duration = sum(b["durationMinutes"] for b in breaks_data)

    work_start_minutes = _time_to_minutes(work_start)
    work_end_minutes = _time_to_minutes(work_end)

    # Handle cross-midnight schedules (e.g. 22:00→06:00)
    crosses_midnight = work_end_minutes <= work_start_minutes
    if crosses_midnight:
        work_end_minutes += 24 * 60

    total_work_minutes = work_end_minutes - work_start_minutes - total_break_duration
    total_work_hours = total_work_minutes / 60.0

    current_minutes = _time_to_minutes(current_time_obj)
    current_norm = _normalize_minute(current_minutes, work_start_minutes, crosses_midnight)

    if current_norm < work_start_minutes:
        hours_used = 0.0
        usage_percentage = 0.0
    elif current_norm >= work_end_minutes:
        hours_used = total_work_hours
        usage_percentage = 100.0
    else:
        work_minutes_elapsed = current_norm - work_start_minutes
        work_minutes_elapsed -= _deduct_breaks(
            breaks_data, current_norm, work_start_minutes, crosses_midnight
        )
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
    day_schedules_data = _load_jsonb(db_record["day_schedules"])
    day_schedules_dict = {
        day: DaySchedule(
            work_hours=WorkHours(**cfg["workHours"]),
            breaks=_parse_breaks_model(cfg),
        )
        for day, cfg in day_schedules_data.items()
    }

    extra_hours_dict = None
    if db_record.get("extra_hours"):
        eh_data = _load_jsonb(db_record["extra_hours"])
        extra_hours_dict = {
            day: [ExtraHour(**h) for h in hours] for day, hours in eh_data.items()
        }

    special_days_dict = None
    if db_record.get("special_days"):
        special_days_dict = _parse_special_days_model(
            _load_jsonb(db_record["special_days"])
        )

    return ScheduleEntity(
        id=db_record["id"],
        device_id=db_record["device_id"],
        shift_type=db_record.get("shift_type", "day"),
        schedule=Schedule(day_schedules=day_schedules_dict),
        extra_hours=extra_hours_dict,
        special_days=special_days_dict,
        valid_from=db_record["valid_from"],
        valid_to=db_record.get("valid_to"),
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
            "shift_type": data.shift_type,
            "day_schedules": _serialize_day_schedules(data.schedule),
            "extra_hours": _serialize_extra_hours(data.extra_hours),
            "special_days": _serialize_special_days(data.special_days),
            "valid_from": data.valid_from,
            "valid_to": data.valid_to,
            "version": data.metadata.version if data.metadata else "1.0",
            "source": data.metadata.source if data.metadata else "ui",
        }

        if schedule_data["valid_to"] is not None:
            schedule_id = await schedule_crud.create_with_split(pool, schedule_data)
        else:
            schedule_id = await schedule_crud.create_with_auto_close(pool, schedule_data)

        db_record = await schedule_crud.get_by_id(pool, schedule_id)
        if not db_record:
            raise RuntimeError("Failed to retrieve created schedule")

        return _build_schedule_read(db_record)

    @staticmethod
    async def get_schedule(
        pool: asyncpg.Pool, device_id: int, target_date: Optional[date] = None,
        shift_type: str = "day",
    ) -> Optional[ScheduleRead]:
        if target_date:
            db_record = await schedule_crud.get_by_device_id_and_date(pool, device_id, target_date, shift_type)
        else:
            db_record = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not db_record:
            return None
        return _build_schedule_read(db_record)

    @staticmethod
    async def get_device_schedules(
        pool: asyncpg.Pool, device_id: int, target_date: Optional[date] = None,
        shift_type: Optional[str] = None,
    ) -> List[ScheduleRead]:
        """Get schedules for a device. Returns all shift types when shift_type is None."""
        if shift_type:
            # Single shift type → delegate to existing method, wrap in list
            result = await ScheduleService.get_schedule(pool, device_id, target_date, shift_type)
            return [result] if result else []

        # All shift types
        if target_date:
            db_records = await schedule_crud.get_all_by_device_id_and_date(pool, device_id, target_date)
        else:
            db_records = await schedule_crud.get_all_current_by_device_id(pool, device_id)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def get_schedule_history(pool: asyncpg.Pool, device_id: int) -> List[ScheduleRead]:
        """Get all schedules (history) for a device."""
        db_records = await schedule_crud.get_all_by_device_id(pool, device_id)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def update_schedule(
        pool: asyncpg.Pool, device_id: int, data: ScheduleUpdate,
        target_date: Optional[date] = None, shift_type: str = "day",
    ) -> ScheduleRead:
        if target_date:
            existing = await schedule_crud.get_by_device_id_and_date(pool, device_id, target_date, shift_type)
        else:
            existing = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not existing:
            raise LookupError(f"Schedule for device_id={device_id} shift_type={shift_type} not found")

        schedule_id = existing["id"]
        update_data: Dict[str, Any] = {
            "day_schedules": _serialize_day_schedules(data.schedule),
            "extra_hours": _serialize_extra_hours(data.extra_hours),
            "special_days": _serialize_special_days(data.special_days),
            "version": data.metadata.version if data.metadata else "1.0",
            "source": data.metadata.source if data.metadata else "ui",
        }

        if data.shift_type is not None:
            update_data["shift_type"] = data.shift_type
        if data.valid_from is not None:
            update_data["valid_from"] = data.valid_from
        if data.valid_to is not None:
            update_data["valid_to"] = data.valid_to

        await schedule_crud.partial_update(pool, schedule_id, update_data)

        db_record = await schedule_crud.get_by_id(pool, schedule_id)
        return _build_schedule_read(db_record)

    @staticmethod
    async def patch_schedule(
        pool: asyncpg.Pool, device_id: int, data: SchedulePatch,
        target_date: Optional[date] = None, shift_type: str = "day",
    ) -> ScheduleRead:
        if target_date:
            existing = await schedule_crud.get_by_device_id_and_date(pool, device_id, target_date, shift_type)
        else:
            existing = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not existing:
            raise LookupError(f"Schedule for device_id={device_id} shift_type={shift_type} not found")

        schedule_id = existing["id"]
        update_data: Dict[str, Any] = {}

        if data.shift_type is not None:
            update_data["shift_type"] = data.shift_type

        if data.schedule is not None:
            update_data["day_schedules"] = _serialize_day_schedules(data.schedule)

        if data.extra_hours is not None:
            update_data["extra_hours"] = _serialize_extra_hours(data.extra_hours)

        if data.special_days is not None:
            update_data["special_days"] = _serialize_special_days(data.special_days)

        if data.valid_from is not None:
            update_data["valid_from"] = data.valid_from

        if data.valid_to is not None:
            update_data["valid_to"] = data.valid_to

        if data.metadata:
            if data.metadata.version:
                update_data["version"] = data.metadata.version
            if data.metadata.source:
                update_data["source"] = data.metadata.source

        if update_data:
            await schedule_crud.partial_update(pool, schedule_id, update_data)

        db_record = await schedule_crud.get_by_id(pool, schedule_id)
        return _build_schedule_read(db_record)

    @staticmethod
    async def get_all_schedules(pool: asyncpg.Pool) -> List[ScheduleRead]:
        db_records = await schedule_crud.get_all_current(pool)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def get_schedules_by_day(pool: asyncpg.Pool, day: str) -> List[ScheduleRead]:
        day_lower = day.lower()
        if day_lower not in VALID_DAYS:
            raise ValueError("Día inválido. Use: monday, tuesday, etc.")
        db_records = await schedule_crud.get_by_day(pool, day_lower)
        return [_build_schedule_read(r) for r in db_records]

    @staticmethod
    async def delete_schedule(
        pool: asyncpg.Pool, device_id: int, schedule_id: Optional[int] = None,
        shift_type: str = "day",
    ) -> bool:
        if schedule_id:
            deleted = await schedule_crud.delete_by_id(pool, schedule_id)
        else:
            deleted = await schedule_crud.delete_current_by_device_id(pool, device_id, shift_type)
        if not deleted:
            raise LookupError(f"Schedule for device_id={device_id} shift_type={shift_type} not found")
        return True

    @staticmethod
    async def get_all_stats(pool: asyncpg.Pool) -> AllScheduleStatsResponse:
        current_time = datetime.now()
        db_records = await schedule_crud.get_all_current(pool)

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
        pool: asyncpg.Pool, device_id: int, shift_type: str = "day",
    ) -> SingleScheduleStatsResponse:
        current_time = datetime.now()
        db_record = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} shift_type={shift_type} not found")

        stats = _calculate_work_hours_usage(db_record, current_time)
        device_stats = ScheduleStatsSchema(**stats)

        return SingleScheduleStatsResponse(
            request_time=current_time.strftime("%H:%M"),
            device_stats=device_stats,
        )

    @staticmethod
    async def get_special_days(
        pool: asyncpg.Pool, device_id: int, shift_type: str = "day",
    ) -> Dict[str, Any]:
        result = await schedule_crud.get_special_days(pool, device_id, shift_type)
        if result is None:
            raise LookupError(f"Schedule for device_id={device_id} shift_type={shift_type} not found")
        return result

    @staticmethod
    async def add_special_day(
        pool: asyncpg.Pool,
        device_id: int,
        date_str: str,
        special_day: SpecialDaySchema,
        shift_type: str = "day",
    ) -> ScheduleRead:
        if not _DATE_RE.match(date_str):
            raise ValueError(_INVALID_DATE_FMT)

        db_record = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        schedule_id = db_record["id"]

        sd_data = db_record.get("special_days")
        if sd_data:
            special_days = json.loads(sd_data) if isinstance(sd_data, str) else sd_data
        else:
            special_days = {}

        special_days[date_str] = special_day.model_dump(by_alias=True)

        await schedule_crud.partial_update(
            pool, schedule_id, {"special_days": json.dumps(special_days)}
        )

        updated = await schedule_crud.get_by_id(pool, schedule_id)
        return _build_schedule_read(updated)

    @staticmethod
    async def delete_special_day(
        pool: asyncpg.Pool, device_id: int, date_str: str,
        shift_type: str = "day",
    ) -> ScheduleDeleteResponse:
        if not _DATE_RE.match(date_str):
            raise ValueError(_INVALID_DATE_FMT)

        db_record = await schedule_crud.get_current_by_device_id(pool, device_id, shift_type)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        schedule_id = db_record["id"]

        sd_data = db_record.get("special_days")
        if not sd_data:
            raise LookupError("No hay días especiales para este dispositivo")

        special_days = json.loads(sd_data) if isinstance(sd_data, str) else sd_data
        if date_str not in special_days:
            raise LookupError("Día especial no encontrado")

        del special_days[date_str]

        await schedule_crud.partial_update(
            pool,
            schedule_id,
            {"special_days": json.dumps(special_days) if special_days else None},
        )

        return ScheduleDeleteResponse(
            message=f"Día especial {date_str} eliminado para device_id={device_id}"
        )

    @staticmethod
    async def get_effective_schedule(
        pool: asyncpg.Pool, device_id: int, date_str: str,
        shift_type: str = "day",
    ) -> Optional[DayScheduleSchema]:
        if not _DATE_RE.match(date_str):
            raise ValueError(_INVALID_DATE_FMT)

        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        db_record = await schedule_crud.get_by_device_id_and_date(pool, device_id, target_date, shift_type)
        if not db_record:
            raise LookupError(f"Schedule for device_id={device_id} not found")

        entity = _db_record_to_entity(db_record)
        effective = entity.get_effective_schedule_for_date(target_date)

        if effective is None:
            return None

        breaks_schema = None
        if effective.breaks:
            breaks_schema = [
                BreakSchema(start=b.start, duration_minutes=b.duration_minutes)
                for b in effective.breaks
            ]

        return DayScheduleSchema(
            work_hours=WorkHoursSchema(
                start=effective.work_hours.start,
                end=effective.work_hours.end,
            ),
            breaks=breaks_schema,
        )


schedule_service = ScheduleService()
