from fastapi import APIRouter, HTTPException, Depends, Header
from typing import List, Optional
import asyncpg
import json
import os
from datetime import datetime, time

from ..schemas.schedule import (
    ScheduleCreateRequest,
    ScheduleResponse,
    ScheduleConfigSchema,
    WorkHoursSchema,
    BreakSchema,
    ExtraHourSchema,
    MetadataSchema,
    ScheduleDeleteResponse,
)
from ..models.schedule import (
    ScheduleEntity,
    Schedule,
    WorkHours,
    Break,
    ExtraHour,
)
from ..repositories.crud import schedule_crud

router = APIRouter(prefix="/schedules", tags=["schedules"])


def parse_time_string(time_str: str) -> time:
    """Convert HH:MM string to Python time object"""
    return datetime.strptime(time_str, "%H:%M").time()


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
        # Preparar datos para la base de datos
        schedule_data = {
            "device_name": schedule_request.device_name,
            "active_days": schedule_request.schedule.active_days,
            "work_start_time": parse_time_string(
                schedule_request.schedule.work_hours.start
            ),
            "work_end_time": parse_time_string(
                schedule_request.schedule.work_hours.end
            ),
            "break_start_time": parse_time_string(
                schedule_request.schedule.break_time.start
            ),
            "break_duration": schedule_request.schedule.break_time.duration_minutes,
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

        # Preparar respuesta
        extra_hours_dict = None
        if db_schedule["extra_hours"]:
            # Parse JSON if it's a string
            extra_hours_data = db_schedule["extra_hours"]
            if isinstance(extra_hours_data, str):
                extra_hours_data = json.loads(extra_hours_data)

            extra_hours_dict = {}
            for day, hours in extra_hours_data.items():
                extra_hours_dict[day] = [ExtraHourSchema(**hour) for hour in hours]

        response = ScheduleResponse(
            id=str(db_schedule["id"]),
            device_name=db_schedule["device_name"],
            schedule=ScheduleConfigSchema(
                active_days=db_schedule["active_days"],
                work_hours=WorkHoursSchema(
                    start=str(db_schedule["work_start_time"]),
                    end=str(db_schedule["work_end_time"]),
                ),
                break_time=BreakSchema(
                    start=str(db_schedule["break_start_time"]),
                    duration_minutes=db_schedule["break_duration"],
                ),
            ),
            extra_hours=extra_hours_dict,
            metadata=MetadataSchema(
                created_at=db_schedule["created_at"],
                version=db_schedule["version"],
                source=db_schedule["source"],
            ),
        )

        return response

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al procesar el horario: {str(e)}"
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

        schedules = []
        for db_schedule in db_schedules:
            # Preparar extra_hours si existe
            extra_hours_dict = None
            if db_schedule["extra_hours"]:
                # Parse JSON if it's a string
                extra_hours_data = db_schedule["extra_hours"]
                if isinstance(extra_hours_data, str):
                    extra_hours_data = json.loads(extra_hours_data)

                extra_hours_dict = {}
                for day_key, hours in extra_hours_data.items():
                    extra_hours_dict[day_key] = [
                        ExtraHourSchema(**hour) for hour in hours
                    ]

            response = ScheduleResponse(
                id=str(db_schedule["id"]),
                device_name=db_schedule["device_name"],
                schedule=ScheduleConfigSchema(
                    active_days=db_schedule["active_days"],
                    work_hours=WorkHoursSchema(
                        start=str(db_schedule["work_start_time"]),
                        end=str(db_schedule["work_end_time"]),
                    ),
                    break_time=BreakSchema(
                        start=str(db_schedule["break_start_time"]),
                        duration_minutes=db_schedule["break_duration"],
                    ),
                ),
                extra_hours=extra_hours_dict,
                metadata=MetadataSchema(
                    created_at=db_schedule["created_at"],
                    version=db_schedule["version"],
                    source=db_schedule["source"],
                ),
            )
            schedules.append(response)

        return schedules

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

        response = ScheduleResponse(
            id=str(db_schedule["id"]),
            device_name=db_schedule["device_name"],
            schedule=ScheduleConfigSchema(
                active_days=db_schedule["active_days"],
                work_hours=WorkHoursSchema(
                    start=str(db_schedule["work_start_time"]),
                    end=str(db_schedule["work_end_time"]),
                ),
                break_time=BreakSchema(
                    start=str(db_schedule["break_start_time"]),
                    duration_minutes=db_schedule["break_duration"],
                ),
            ),
            extra_hours=extra_hours_dict,
            metadata=MetadataSchema(
                created_at=db_schedule["created_at"],
                version=db_schedule["version"],
                source=db_schedule["source"],
            ),
        )

        return response

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

        schedules = []
        for db_schedule in db_schedules:
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

            response = ScheduleResponse(
                id=str(db_schedule["id"]),
                device_name=db_schedule["device_name"],
                schedule=ScheduleConfigSchema(
                    active_days=db_schedule["active_days"],
                    work_hours=WorkHoursSchema(
                        start=str(db_schedule["work_start_time"]),
                        end=str(db_schedule["work_end_time"]),
                    ),
                    break_time=BreakSchema(
                        start=str(db_schedule["break_start_time"]),
                        duration_minutes=db_schedule["break_duration"],
                    ),
                ),
                extra_hours=extra_hours_dict,
                metadata=MetadataSchema(
                    created_at=db_schedule["created_at"],
                    version=db_schedule["version"],
                    source=db_schedule["source"],
                ),
            )
            schedules.append(response)

        return schedules

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
