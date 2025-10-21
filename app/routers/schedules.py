from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from typing import List, Optional
import json
import os
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

    return ScheduleResponse(
        id=str(db_schedule["id"]),
        device_name=db_schedule["device_name"],
        schedule=day_schedules_dict,
        extra_hours=extra_hours_dict,
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


@router.post("/", response_model=ScheduleResponse)
async def create_or_update_schedule(
    schedule_request: ScheduleCreateRequest,
    api_key_valid: None = Depends(verify_api_key),
):
    """
    Crear o actualizar un horario para un dispositivo.

    Recibe el JSON completo del horario y lo almacena en la base de datos.
    El ID se genera automáticamente y extraHours puede ser null.
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
            "version": (
                schedule_request.metadata.version
                if schedule_request.metadata
                else "1.0"
            ),
            "source": (
                schedule_request.metadata.source if schedule_request.metadata else "api"
            ),
        }

        # Insertar/actualizar horario
        await schedule_crud.create_or_update(schedule_data)

        # Obtener el horario insertado con su ID
        db_schedule = await schedule_crud.get_by_device_name(
            schedule_request.device_name
        )

        if not db_schedule:
            raise HTTPException(status_code=500, detail="Error al crear el horario")

        return build_schedule_response(db_schedule)

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

        # Preparar datos para la base de datos (igual que en POST)
        schedule_data = {
            "device_name": device_name,
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
            "version": (
                schedule_request.metadata.version
                if schedule_request.metadata
                else "1.0"
            ),
            "source": (
                schedule_request.metadata.source if schedule_request.metadata else "api"
            ),
        }

        # Actualizar horario
        await schedule_crud.create_or_update(schedule_data)

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

        if patch_request.metadata:
            if patch_request.metadata.version:
                update_data["version"] = patch_request.metadata.version
            if patch_request.metadata.source:
                update_data["source"] = patch_request.metadata.source

        # Actualizar solo los campos especificados
        updated = await schedule_crud.partial_update(device_name, update_data)
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
async def get_all_schedules_endpoint(api_key_valid: None = Depends(verify_api_key)):
    """
    Obtener todos los horarios registrados.
    """
    try:
        db_schedules = await schedule_crud.get_all()
        return [build_schedule_response(db_schedule) for db_schedule in db_schedules]

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al obtener los horarios: {str(e)}"
        )


@router.delete("/{device_name}", response_model=ScheduleDeleteResponse)
async def delete_schedule_endpoint(
    device_name: str, api_key_valid: None = Depends(verify_api_key)
):
    """
    Eliminar el horario de un dispositivo específico.
    """
    try:
        deleted = await schedule_crud.delete_by_device_name(device_name)

        if not deleted:
            raise HTTPException(status_code=404, detail="Horario no encontrado")

        return ScheduleDeleteResponse(
            message=f"Horario del dispositivo {device_name} eliminado correctamente"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al eliminar el horario: {str(e)}"
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


@router.get("/docs", include_in_schema=False)
async def get_schedules_docs():
    """
    Get Swagger UI documentation for schedules API.

    This provides interactive API documentation specifically for schedule endpoints.
    """
    return get_swagger_ui_html(
        openapi_url="/schedules/openapi.json",
        title="Schedules API - Swagger UI",
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
        openapi_url="/schedules/openapi.json",
        title="Schedules API - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.0/bundles/redoc.standalone.js",
    )


@router.get("/openapi.json", include_in_schema=False)
async def get_schedules_openapi():
    """
    Get OpenAPI JSON schema for schedules API.

    This provides the OpenAPI specification specifically for schedule endpoints.
    """
    return get_openapi(
        title="Schedules API",
        version="1.0.0",
        description="API for managing work schedules and shifts with statistics",
        routes=router.routes,
    )
