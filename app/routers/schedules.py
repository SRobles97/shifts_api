from fastapi import APIRouter, HTTPException, Depends, Header, Query, Body
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from typing import List, Optional, Dict, Any
import json
import os
import re
from datetime import datetime, time

from ..schemas.schedule import (
    ScheduleCreateRequest,
    ScheduleUpdateRequest,
    SchedulePatchRequest,
    ScheduleResponse,
    WorkHoursSchema,
    BreakSchema,
    ExtraHourSchema,
    MetadataSchema,
    ScheduleDeleteResponse,
    ScheduleStatsSchema,
    AllScheduleStatsResponse,
    SingleScheduleStatsResponse,
    DayScheduleSchema,
    SpecialDaySchema,
)
from ..repositories.crud import schedule_crud

router = APIRouter(prefix="/schedules", tags=["schedules"])


def parse_time_string(time_str: str) -> time:
    """Convert HH:MM string to Python time object"""
    return datetime.strptime(time_str, "%H:%M").time()


def time_to_minutes(time_obj: time) -> int:
    """Convert time object to minutes since midnight"""
    return time_obj.hour * 60 + time_obj.minute


def calculate_work_hours_usage(db_schedule: dict, current_time: datetime) -> dict:
    """
    Calculate work hours usage statistics for a schedule.

    Args:
        db_schedule: Database schedule record
        current_time: Current datetime

    Returns:
        Dictionary with usage statistics
    """
    current_day = current_time.strftime("%A").lower()
    current_time_obj = current_time.time()
    current_time_str = current_time_obj.strftime("%H:%M")

    # Parse day_schedules from JSONB
    day_schedules_data = db_schedule["day_schedules"]
    if isinstance(day_schedules_data, str):
        day_schedules_data = json.loads(day_schedules_data)

    # Check if device is active today
    if current_day not in day_schedules_data:
        return {
            "device_name": db_schedule["device_name"],
            "schedule_start": "00:00",
            "schedule_end": "00:00",
            "current_time": current_time_str,
            "hours_used": 0.0,
            "total_work_hours": 0.0,
            "usage_percentage": 0.0,
        }

    # Get today's schedule
    today_schedule = day_schedules_data[current_day]
    work_start = parse_time_string(today_schedule["workHours"]["start"])
    work_end = parse_time_string(today_schedule["workHours"]["end"])
    break_start = parse_time_string(today_schedule["break"]["start"])
    break_duration = today_schedule["break"]["durationMinutes"]

    # Calculate total work hours for the day (excluding break)
    work_start_minutes = time_to_minutes(work_start)
    work_end_minutes = time_to_minutes(work_end)
    total_work_minutes = work_end_minutes - work_start_minutes - break_duration
    total_work_hours = total_work_minutes / 60.0

    # Calculate hours used so far
    current_minutes = time_to_minutes(current_time_obj)
    hours_used = 0.0

    if current_minutes < work_start_minutes:
        # Before work starts
        hours_used = 0.0
        usage_percentage = 0.0
    elif current_minutes >= work_end_minutes:
        # After work ends - full day worked
        hours_used = total_work_hours
        usage_percentage = 100.0
    else:
        # During work hours - calculate partial usage
        work_minutes_elapsed = current_minutes - work_start_minutes

        # Subtract break time if we've passed the break
        break_start_minutes = time_to_minutes(break_start)
        if current_minutes > break_start_minutes + break_duration:
            # Passed the entire break
            work_minutes_elapsed -= break_duration
        elif current_minutes > break_start_minutes:
            # Currently in break - subtract partial break time
            work_minutes_elapsed -= current_minutes - break_start_minutes

        hours_used = max(0, work_minutes_elapsed) / 60.0
        usage_percentage = (
            (hours_used / total_work_hours * 100) if total_work_hours > 0 else 0.0
        )

    # Cap usage percentage at 100%
    usage_percentage = min(100.0, max(0.0, usage_percentage))

    return {
        "device_name": db_schedule["device_name"],
        "schedule_start": work_start.strftime("%H:%M"),
        "schedule_end": work_end.strftime("%H:%M"),
        "current_time": current_time_str,
        "hours_used": round(hours_used, 2),
        "total_work_hours": round(total_work_hours, 2),
        "usage_percentage": round(usage_percentage, 2),
    }


def build_schedule_response(db_schedule: dict) -> ScheduleResponse:
    """
    Build a ScheduleResponse from database record.

    Args:
        db_schedule: Database record containing schedule data

    Returns:
        ScheduleResponse object
    """
    # Parse day_schedules from JSONB
    day_schedules_data = db_schedule["day_schedules"]
    if isinstance(day_schedules_data, str):
        day_schedules_data = json.loads(day_schedules_data)

    # Convert to DayScheduleSchema objects
    from ..schemas.schedule import DayScheduleSchema

    day_schedules_dict = {}
    for day, day_config in day_schedules_data.items():
        day_schedules_dict[day] = DayScheduleSchema(
            work_hours=WorkHoursSchema(**day_config["workHours"]),
            break_time=BreakSchema(**day_config["break"]),
        )

    # Preparar extra_hours si existe
    extra_hours_dict = None
    if db_schedule["extra_hours"]:
        # Parse JSON if it's a string
        extra_hours_data = db_schedule["extra_hours"]
        if isinstance(extra_hours_data, str):
            extra_hours_data = json.loads(extra_hours_data)

        extra_hours_dict = {}
        for day, hours in extra_hours_data.items():
            extra_hours_dict[day] = [ExtraHourSchema(**hour) for hour in hours]

    # Preparar special_days si existe
    special_days_dict = None
    if db_schedule.get("special_days"):
        # Parse JSON if it's a string
        special_days_data = db_schedule["special_days"]
        if isinstance(special_days_data, str):
            special_days_data = json.loads(special_days_data)

        from ..schemas.schedule import SpecialDaySchema
        special_days_dict = {}
        for date_str, special_day in special_days_data.items():
            # Parse work_hours and break_time if they exist
            work_hours = None
            if special_day.get("workHours"):
                work_hours = WorkHoursSchema(**special_day["workHours"])

            break_time = None
            if special_day.get("break"):
                break_time = BreakSchema(**special_day["break"])

            special_days_dict[date_str] = SpecialDaySchema(
                name=special_day["name"],
                type=special_day["type"],
                work_hours=work_hours,
                break_time=break_time,
                is_recurring=special_day.get("isRecurring", False),
                recurrence_pattern=special_day.get("recurrencePattern"),
            )

    return ScheduleResponse(
        id=str(db_schedule["id"]),
        device_name=db_schedule["device_name"],
        schedule=day_schedules_dict,
        extra_hours=extra_hours_dict,
        special_days=special_days_dict,
        start_date=db_schedule["start_date"],
        end_date=db_schedule["end_date"],
        metadata=MetadataSchema(
            created_at=db_schedule["created_at"],
            version=db_schedule["version"],
            source=db_schedule["source"],
        ),
    )


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Verificar que el API key proporcionado en el header sea válido.

    Args:
        x_api_key: API key proporcionado en el header X-API-Key

    Raises:
        HTTPException: Si el API key es inválido o no se proporciona
    """
    expected_api_key = os.getenv("API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=500, detail="API key no configurada en el servidor"
        )

    if x_api_key != expected_api_key:
        raise HTTPException(
            status_code=401,
            detail="API key inválida",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ========== Documentation Endpoints (must be before /{device_name} routes) ==========


@router.get("/openapi.json", include_in_schema=False)
async def get_schedules_openapi():
    """
    Get OpenAPI JSON schema for schedules API only.

    Generates OpenAPI spec with correct /shifts-api/v1/schedules prefix.
    """
    # Generate base OpenAPI from router routes
    openapi_schema = get_openapi(
        title="Shifts API - Schedule Management",
        version="1.0.0",
        description="API for managing work schedules and shifts for devices",
        routes=router.routes,
    )

    # Add the correct prefix to all paths
    # Router already has /schedules prefix, so we only add /shifts-api/v1
    prefix = "/shifts-api/v1"
    prefixed_paths = {}
    for path, path_item in openapi_schema.get("paths", {}).items():
        # Skip the docs/redoc/openapi endpoints themselves
        if path in ["/schedules/docs", "/schedules/redoc", "/schedules/openapi.json"]:
            continue
        prefixed_paths[f"{prefix}{path}"] = path_item

    openapi_schema["paths"] = prefixed_paths
    return openapi_schema


@router.get("/docs", include_in_schema=False)
async def get_schedules_docs():
    """
    Get Swagger UI documentation for schedules API.

    This provides interactive API documentation specifically for schedule endpoints.
    """
    return get_swagger_ui_html(
        openapi_url="/shifts-api/v1/schedules/openapi.json",
        title="Shifts API - Swagger UI",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
    )


@router.get("/redoc", include_in_schema=False)
async def get_schedules_redoc():
    """
    Get ReDoc documentation for schedules API.

    This provides clean, readable API documentation specifically for schedule endpoints.
    """
    return get_redoc_html(
        openapi_url="/shifts-api/v1/schedules/openapi.json",
        title="Shifts API - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.0/bundles/redoc.standalone.js",
    )


# ========== Schedule CRUD Endpoints ==========


@router.post("/", response_model=ScheduleResponse)
async def create_or_update_schedule(
    schedule_request: ScheduleCreateRequest,
    auto_close: bool = Query(
        False,
        alias="autoClose",
        description="If true, automatically close overlapping schedules by setting their end_date to the day before this schedule's start_date"
    ),
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Crear o actualizar un horario para un dispositivo.

    Recibe el JSON completo del horario y lo almacena en la base de datos.
    El ID se genera automáticamente y extraHours puede ser null.

    Query Parameters:
    - autoClose: Si es true, cierra automáticamente los horarios existentes que se solapan
                 estableciendo su end_date al día anterior al start_date del nuevo horario.
    """
    try:
        # Convert day schedules to JSONB format
        day_schedules_json = {}
        for day, day_schedule in schedule_request.schedule.items():
            day_schedules_json[day] = {
                "workHours": {
                    "start": day_schedule.work_hours.start,
                    "end": day_schedule.work_hours.end,
                },
                "break": {
                    "start": day_schedule.break_time.start,
                    "durationMinutes": day_schedule.break_time.duration_minutes,
                },
            }

        # Preparar special_days si existe
        special_days_json = None
        if schedule_request.special_days:
            special_days_json = json.dumps(
                {
                    date_str: special_day.model_dump(by_alias=True)
                    for date_str, special_day in schedule_request.special_days.items()
                }
            )

        # Preparar datos para la base de datos
        schedule_data = {
            "device_name": schedule_request.device_name,
            "day_schedules": json.dumps(day_schedules_json),
            "extra_hours": (
                json.dumps(
                    {
                        day: [hour.model_dump() for hour in hours]
                        for day, hours in schedule_request.extra_hours.items()
                    }
                )
                if schedule_request.extra_hours
                else None
            ),
            "special_days": special_days_json,
            "start_date": schedule_request.start_date,
            "end_date": schedule_request.end_date,
            "version": (
                schedule_request.metadata.version
                if schedule_request.metadata
                else "1.0"
            ),
            "source": (
                schedule_request.metadata.source if schedule_request.metadata else "api"
            ),
        }

        # Crear horario (returns schedule ID)
        try:
            schedule_id = await schedule_crud.create_or_update(
                schedule_data, auto_close_existing=auto_close
            )
        except ValueError as e:
            # Date range overlap error
            raise HTTPException(status_code=400, detail=str(e))

        # Obtener el horario creado por ID
        db_schedule = await schedule_crud.get_by_device_name_and_id(
            schedule_request.device_name, schedule_id
        )

        if not db_schedule:
            raise HTTPException(status_code=500, detail="Error al crear el horario")

        return build_schedule_response(db_schedule)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al procesar el horario: {str(e)}"
        )


@router.put("/{device_name}", response_model=ScheduleResponse)
async def update_schedule(
    device_name: str,
    schedule_request: ScheduleUpdateRequest,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Actualizar completamente un horario existente (PUT).

    Requiere que el dispositivo exista previamente y actualiza todos los campos.
    """
    try:
        # Verificar que el schedule existe
        existing_schedule = await schedule_crud.get_by_device_name(device_name)
        if not existing_schedule:
            raise HTTPException(status_code=404, detail="Horario no encontrado")

        # Verificar que device_name coincide
        if schedule_request.device_name != device_name:
            raise HTTPException(
                status_code=400,
                detail="El nombre del dispositivo no coincide con la URL",
            )

        # Convert day schedules to JSONB format
        day_schedules_json = {}
        for day, day_schedule in schedule_request.schedule.items():
            day_schedules_json[day] = {
                "workHours": {
                    "start": day_schedule.work_hours.start,
                    "end": day_schedule.work_hours.end,
                },
                "break": {
                    "start": day_schedule.break_time.start,
                    "durationMinutes": day_schedule.break_time.duration_minutes,
                },
            }

        # Preparar special_days si existe
        special_days_json = None
        if schedule_request.special_days:
            special_days_json = json.dumps(
                {
                    date_str: special_day.model_dump(by_alias=True)
                    for date_str, special_day in schedule_request.special_days.items()
                }
            )

        # Preparar datos para la base de datos
        update_data = {
            "day_schedules": json.dumps(day_schedules_json),
            "extra_hours": (
                json.dumps(
                    {
                        day: [hour.model_dump() for hour in hours]
                        for day, hours in schedule_request.extra_hours.items()
                    }
                )
                if schedule_request.extra_hours
                else None
            ),
            "special_days": special_days_json,
            "start_date": schedule_request.start_date,
            "end_date": schedule_request.end_date,
            "version": (
                schedule_request.metadata.version
                if schedule_request.metadata
                else "1.0"
            ),
            "source": (
                schedule_request.metadata.source if schedule_request.metadata else "api"
            ),
        }

        # Actualizar horario existente
        await schedule_crud.partial_update(device_name, existing_schedule["id"], update_data)

        # Obtener el horario actualizado
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        return build_schedule_response(db_schedule)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al actualizar el horario: {str(e)}"
        )


@router.patch("/{device_name}", response_model=ScheduleResponse)
async def patch_schedule(
    device_name: str,
    patch_request: SchedulePatchRequest,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Actualizar parcialmente un horario existente (PATCH).

    Permite actualizar solo los campos especificados sin afectar los demás.
    """
    try:
        # Verificar que el schedule existe
        existing_schedule = await schedule_crud.get_by_device_name(device_name)
        if not existing_schedule:
            raise HTTPException(status_code=404, detail="Horario no encontrado")

        # Preparar datos de actualización parcial
        update_data = {}

        if patch_request.schedule:
            # Convert day schedules to JSONB format
            day_schedules_json = {}
            for day, day_schedule in patch_request.schedule.items():
                day_schedules_json[day] = {
                    "workHours": {
                        "start": day_schedule.work_hours.start,
                        "end": day_schedule.work_hours.end,
                    },
                    "break": {
                        "start": day_schedule.break_time.start,
                        "durationMinutes": day_schedule.break_time.duration_minutes,
                    },
                }
            update_data["day_schedules"] = json.dumps(day_schedules_json)

        if patch_request.extra_hours is not None:
            update_data["extra_hours"] = (
                json.dumps(
                    {
                        day: [hour.model_dump() for hour in hours]
                        for day, hours in patch_request.extra_hours.items()
                    }
                )
                if patch_request.extra_hours
                else None
            )

        if patch_request.special_days is not None:
            update_data["special_days"] = (
                json.dumps(
                    {
                        date_str: special_day.model_dump(by_alias=True)
                        for date_str, special_day in patch_request.special_days.items()
                    }
                )
                if patch_request.special_days
                else None
            )

        if patch_request.metadata:
            if patch_request.metadata.version:
                update_data["version"] = patch_request.metadata.version
            if patch_request.metadata.source:
                update_data["source"] = patch_request.metadata.source

        # Actualizar solo los campos especificados
        updated = await schedule_crud.partial_update(device_name, existing_schedule["id"], update_data)
        if not updated:
            raise HTTPException(status_code=404, detail="Horario no encontrado")

        # Obtener el horario actualizado
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        return build_schedule_response(db_schedule)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al actualizar parcialmente el horario: {str(e)}",
        )


@router.get("/by-day/{day}", response_model=List[ScheduleResponse])
async def get_schedules_by_day_endpoint(
    day: str, api_key_valid: None = Depends(verify_api_key)
):
    """
    Obtener todos los horarios que incluyen un día específico de la semana.

    Args:
        day: Día de la semana (monday, tuesday, wednesday, thursday, friday, saturday, sunday)
    """
    try:
        valid_days = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
        if day.lower() not in valid_days:
            raise HTTPException(
                status_code=400, detail="Día inválido. Use: monday, tuesday, etc."
            )

        db_schedules = await schedule_crud.get_by_day(day.lower())

        return [build_schedule_response(db_schedule) for db_schedule in db_schedules]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener los horarios por día: {str(e)}"
        )


@router.get("/{device_name}", response_model=Optional[ScheduleResponse])
async def get_schedule(device_name: str, api_key_valid: None = Depends(verify_api_key)):
    """
    Obtener el horario de un dispositivo específico.
    """
    try:
        db_schedule = await schedule_crud.get_by_device_name(device_name)

        if not db_schedule:
            return None

        return build_schedule_response(db_schedule)

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener el horario: {str(e)}"
        )


@router.get("/", response_model=List[ScheduleResponse])
async def get_all_schedules_endpoint(
    include_inactive: bool = Query(
        False,
        alias="includeInactive",
        description="Include schedules that have ended (past end_date)"
    ),
    api_key_valid: None = Depends(verify_api_key)
):
    """
    Obtener todos los horarios.

    Por defecto, devuelve solo los horarios actualmente activos (uno por dispositivo).
    Usa includeInactive=true para ver todos los horarios incluyendo los pasados.
    """
    try:
        db_schedules = await schedule_crud.get_all(include_inactive=include_inactive)
        return [build_schedule_response(db_schedule) for db_schedule in db_schedules]

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener los horarios: {str(e)}"
        )


@router.delete("/{device_name}", response_model=ScheduleDeleteResponse)
async def delete_schedule_endpoint(
    device_name: str,
    schedule_id: Optional[int] = Query(
        None,
        alias="scheduleId",
        description="Specific schedule ID to delete. If not provided, deletes ALL schedules for device."
    ),
    api_key_valid: None = Depends(verify_api_key)
):
    """
    Eliminar horario(s) de un dispositivo.

    Query Parameters:
    - scheduleId: ID específico del horario a eliminar

    Comportamiento:
    - Con scheduleId: Elimina solo ese horario específico
    - Sin scheduleId: Elimina TODOS los horarios del dispositivo (usar con precaución!)
    """
    try:
        deleted = await schedule_crud.delete_by_device_name(device_name, schedule_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="Horario no encontrado")

        if schedule_id:
            message = f"Horario {schedule_id} del dispositivo {device_name} eliminado correctamente"
        else:
            message = f"Todos los horarios del dispositivo {device_name} eliminados correctamente"

        return ScheduleDeleteResponse(message=message)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al eliminar el horario: {str(e)}"
        )


@router.get("/{device_name}/history", response_model=List[ScheduleResponse])
async def get_device_schedule_history(
    device_name: str,
    include_past: bool = Query(
        False,
        alias="includePast",
        description="Include schedules that have ended"
    ),
    api_key_valid: None = Depends(verify_api_key)
):
    """
    Obtener todas las configuraciones de horarios para un dispositivo.

    Devuelve todos los horarios para este dispositivo ordenados por start_date (más reciente primero).
    Útil para ver el historial de horarios y planificar cambios futuros.

    Query Parameters:
    - includePast: boolean (default: false) - Incluir horarios que ya finalizaron
    """
    try:
        db_schedules = await schedule_crud.get_all_by_device_name(
            device_name, include_past=include_past
        )

        if not db_schedules:
            return []

        return [build_schedule_response(db_schedule) for db_schedule in db_schedules]

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al obtener historial de horarios: {str(e)}"
        )


@router.get("/{device_name}/on-date/{date}", response_model=Optional[ScheduleResponse])
async def get_schedule_on_date(
    device_name: str,
    date: str,
    api_key_valid: None = Depends(verify_api_key)
):
    """
    Obtener el horario activo para un dispositivo en una fecha específica.

    Útil para verificar horarios históricos o planificar cambios futuros.

    Path Parameters:
    - device_name: Identificador del dispositivo
    - date: Fecha en formato ISO (YYYY-MM-DD)
    """
    try:
        # Validar formato de fecha
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(
                status_code=400,
                detail="Formato de fecha inválido. Usar YYYY-MM-DD"
            )

        from datetime import datetime as dt
        date_obj = dt.strptime(date, "%Y-%m-%d").date()

        db_schedule = await schedule_crud.get_schedule_for_device_on_date(
            device_name, date_obj
        )

        if not db_schedule:
            return None

        return build_schedule_response(db_schedule)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al obtener horario para la fecha: {str(e)}"
        )


@router.get("/stats/all", response_model=AllScheduleStatsResponse)
async def get_all_schedule_stats(api_key_valid: None = Depends(verify_api_key)):
    """
    Get work hour usage statistics for all devices.

    Returns schedule start/end times, current time, hours used so far,
    and usage percentage for all devices.
    """
    try:
        current_time = datetime.now()
        db_schedules = await schedule_crud.get_all()

        if not db_schedules:
            return AllScheduleStatsResponse(
                request_time=current_time.strftime("%H:%M"), devices=[]
            )

        device_stats = []
        for db_schedule in db_schedules:
            stats = calculate_work_hours_usage(db_schedule, current_time)
            device_stats.append(ScheduleStatsSchema(**stats))

        return AllScheduleStatsResponse(
            request_time=current_time.strftime("%H:%M"), devices=device_stats
        )

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener estadísticas: {str(e)}"
        )


@router.get("/stats/{device_name}", response_model=SingleScheduleStatsResponse)
async def get_device_schedule_stats(
    device_name: str, api_key_valid: None = Depends(verify_api_key)
):
    """
    Get work hour usage statistics for a specific device.

    Returns schedule start/end times, current time, hours used so far,
    and usage percentage for the requested device.
    """
    try:
        current_time = datetime.now()
        db_schedule = await schedule_crud.get_by_device_name(device_name)

        if not db_schedule:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")

        stats = calculate_work_hours_usage(db_schedule, current_time)
        device_stats = ScheduleStatsSchema(**stats)

        return SingleScheduleStatsResponse(
            request_time=current_time.strftime("%H:%M"), device_stats=device_stats
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al obtener estadísticas del dispositivo: {str(e)}",
        )


# ========== Special Days Endpoints ==========


@router.get("/special-days/{device_name}", response_model=Dict[str, Any])
async def get_device_special_days(
    device_name: str,
    start_date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$', alias="startDate"),
    end_date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$', alias="endDate"),
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Get special days for a device, optionally filtered by date range.

    Query Parameters:
    - startDate: Optional ISO date (YYYY-MM-DD) - start of range
    - endDate: Optional ISO date (YYYY-MM-DD) - end of range
    """
    try:
        # If date range provided, use get_special_days_in_range
        if start_date and end_date:
            special_days = await schedule_crud.get_special_days_in_range(
                device_name, start_date, end_date
            )
            if special_days is None:
                raise HTTPException(status_code=404, detail="Dispositivo no encontrado")
            return special_days

        # Otherwise, get all special days for the device
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        if not db_schedule:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")

        special_days_data = db_schedule.get("special_days")
        if not special_days_data:
            return {}

        # Parse JSON if string
        if isinstance(special_days_data, str):
            return json.loads(special_days_data)
        return special_days_data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener días especiales: {str(e)}"
        )


@router.post("/special-days/{device_name}", response_model=ScheduleResponse)
async def add_special_day(
    device_name: str,
    date: str = Query(..., pattern=r'^\d{4}-\d{2}-\d{2}$'),
    special_day: SpecialDaySchema = Body(...),
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Add or update a single special day for a device.

    Query Parameters:
    - date: ISO date (YYYY-MM-DD) for the special day
    """
    try:

        # Get existing schedule
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        if not db_schedule:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")

        # Parse existing special_days
        special_days_data = db_schedule.get("special_days")
        if special_days_data:
            if isinstance(special_days_data, str):
                special_days = json.loads(special_days_data)
            else:
                special_days = special_days_data
        else:
            special_days = {}

        # Add or update the special day
        special_days[date] = special_day.model_dump(by_alias=True)

        # Update in database
        update_data = {"special_days": json.dumps(special_days)}
        await schedule_crud.partial_update(device_name, update_data)

        # Return updated schedule
        updated_schedule = await schedule_crud.get_by_device_name(device_name)
        return build_schedule_response(updated_schedule)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al añadir día especial: {str(e)}"
        )


@router.delete("/special-days/{device_name}/{date}", response_model=ScheduleDeleteResponse)
async def delete_special_day(
    device_name: str,
    date: str,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Delete a specific special day for a device.

    Path Parameters:
    - device_name: Device identifier
    - date: ISO date (YYYY-MM-DD) of the special day to delete
    """
    try:
        # Validate date format
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(
                status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD"
            )

        # Get existing schedule
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        if not db_schedule:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")

        # Parse existing special_days
        special_days_data = db_schedule.get("special_days")
        if not special_days_data:
            raise HTTPException(
                status_code=404, detail="No hay días especiales para este dispositivo"
            )

        if isinstance(special_days_data, str):
            special_days = json.loads(special_days_data)
        else:
            special_days = special_days_data

        # Check if date exists
        if date not in special_days:
            raise HTTPException(status_code=404, detail="Día especial no encontrado")

        # Remove the special day
        del special_days[date]

        # Update in database
        update_data = {
            "special_days": json.dumps(special_days) if special_days else None
        }
        await schedule_crud.partial_update(device_name, update_data)

        return ScheduleDeleteResponse(
            message=f"Día especial {date} eliminado para {device_name}"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al eliminar día especial: {str(e)}"
        )


@router.get("/effective-schedule/{device_name}/{date}", response_model=Optional[DayScheduleSchema])
async def get_effective_schedule(
    device_name: str,
    date: str,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Get the effective schedule for a device on a specific date.

    Returns the actual schedule considering:
    1. Special days (exact match)
    2. Recurring special days (annual)
    3. Regular weekday schedule

    Path Parameters:
    - device_name: Device identifier
    - date: ISO date (YYYY-MM-DD)

    Returns:
    - DayScheduleSchema if there's work scheduled
    - null if no work on that date
    """
    try:
        # Validate date format
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(
                status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD"
            )

        from datetime import datetime
        from ..models.schedule import Schedule, DaySchedule, WorkHours, Break, SpecialDay, ExtraHour, ScheduleEntity

        # Get device schedule
        db_schedule = await schedule_crud.get_by_device_name(device_name)
        if not db_schedule:
            raise HTTPException(status_code=404, detail="Dispositivo no encontrado")

        # Parse to ScheduleEntity model
        day_schedules_data = db_schedule["day_schedules"]
        if isinstance(day_schedules_data, str):
            day_schedules_data = json.loads(day_schedules_data)

        # Convert to model objects
        day_schedules_dict = {}
        for day, day_config in day_schedules_data.items():
            day_schedules_dict[day] = DaySchedule(
                work_hours=WorkHours(**day_config["workHours"]),
                break_time=Break(**day_config["break"]),
            )

        schedule = Schedule(day_schedules=day_schedules_dict)

        # Parse extra_hours if present
        extra_hours_dict = None
        if db_schedule.get("extra_hours"):
            extra_hours_data = db_schedule["extra_hours"]
            if isinstance(extra_hours_data, str):
                extra_hours_data = json.loads(extra_hours_data)
            extra_hours_dict = {
                day: [ExtraHour(**hour) for hour in hours]
                for day, hours in extra_hours_data.items()
            }

        # Parse special_days if present
        special_days_dict = None
        if db_schedule.get("special_days"):
            special_days_data = db_schedule["special_days"]
            if isinstance(special_days_data, str):
                special_days_data = json.loads(special_days_data)
            special_days_dict = {
                date_str: SpecialDay(
                    name=sd["name"],
                    type=sd["type"],
                    work_hours=WorkHours(**sd["workHours"]) if sd.get("workHours") else None,
                    break_time=Break(**sd["break"]) if sd.get("break") else None,
                    is_recurring=sd.get("isRecurring", False),
                    recurrence_pattern=sd.get("recurrencePattern"),
                )
                for date_str, sd in special_days_data.items()
            }

        # Create ScheduleEntity
        entity = ScheduleEntity(
            id=db_schedule["id"],
            device_name=device_name,
            schedule=schedule,
            extra_hours=extra_hours_dict,
            special_days=special_days_dict,
            created_at=db_schedule["created_at"],
            updated_at=db_schedule["updated_at"],
            version=db_schedule["version"],
            source=db_schedule["source"],
        )

        # Get effective schedule for the date
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        effective = entity.get_effective_schedule_for_date(target_date)

        if effective is None:
            return None

        # Convert to DayScheduleSchema
        from ..schemas.schedule import DayScheduleSchema
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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener horario efectivo: {str(e)}"
        )


@router.get("/active-devices/{date}", response_model=List[ScheduleResponse])
async def get_active_devices_on_date(
    date: str,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Get all devices with active schedules on a specific date.

    Path Parameters:
    - date: ISO date (YYYY-MM-DD)
    """
    try:
        # Validate date format
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            raise HTTPException(
                status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD"
            )

        # Get schedules active on this date
        schedules = await schedule_crud.get_schedules_for_date(date)

        # Convert to response format
        return [build_schedule_response(schedule) for schedule in schedules]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al obtener dispositivos activos: {str(e)}",
        )
