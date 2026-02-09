"""
Schedule API router — thin controllers delegating to the service layer.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from typing import List, Optional, Dict, Any

import asyncpg

from ..core.dependencies import verify_api_key, get_db_pool
from ..schemas.schedule import (
    ScheduleCreate,
    ScheduleUpdate,
    SchedulePatch,
    ScheduleRead,
    ScheduleDeleteResponse,
    AllScheduleStatsResponse,
    SingleScheduleStatsResponse,
    DayScheduleSchema,
    SpecialDaySchema,
)
from ..services.schedule_service import schedule_service

router = APIRouter(prefix="/schedules", tags=["schedules"])


# ========== Documentation Endpoints ==========


@router.get("/openapi.json", include_in_schema=False)
async def get_schedules_openapi():
    """Get OpenAPI JSON schema for schedules API only."""
    openapi_schema = get_openapi(
        title="Shifts API - Schedule Management",
        version="1.0.0",
        description="API for managing work schedules and shifts for devices",
        routes=router.routes,
    )

    prefix = "/shifts-api/v1"
    prefixed_paths = {}
    for path, path_item in openapi_schema.get("paths", {}).items():
        if path in ["/schedules/docs", "/schedules/redoc", "/schedules/openapi.json"]:
            continue
        prefixed_paths[f"{prefix}{path}"] = path_item

    openapi_schema["paths"] = prefixed_paths
    return openapi_schema


@router.get("/docs", include_in_schema=False)
async def get_schedules_docs():
    """Get Swagger UI documentation for schedules API."""
    return get_swagger_ui_html(
        openapi_url="/shifts-api/v1/schedules/openapi.json",
        title="Shifts API - Swagger UI",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
    )


@router.get("/redoc", include_in_schema=False)
async def get_schedules_redoc():
    """Get ReDoc documentation for schedules API."""
    return get_redoc_html(
        openapi_url="/shifts-api/v1/schedules/openapi.json",
        title="Shifts API - ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2.1.0/bundles/redoc.standalone.js",
    )


# ========== Schedule CRUD Endpoints ==========


@router.post("/", response_model=ScheduleRead)
async def create_schedule(
    data: ScheduleCreate,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Create or upsert a schedule for a device."""
    try:
        return await schedule_service.create_schedule(pool, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear el horario: {e}")


@router.put("/{device_id}", response_model=ScheduleRead)
async def update_schedule(
    device_id: int,
    data: ScheduleUpdate,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Full replacement of a schedule for a device."""
    try:
        return await schedule_service.update_schedule(pool, device_id, data)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar el horario: {e}")


@router.patch("/{device_id}", response_model=ScheduleRead)
async def patch_schedule(
    device_id: int,
    data: SchedulePatch,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Partial update of a schedule for a device."""
    try:
        return await schedule_service.patch_schedule(pool, device_id, data)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar parcialmente: {e}")


@router.get("/by-day/{day}", response_model=List[ScheduleRead])
async def get_schedules_by_day(
    day: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get all schedules active on a specific day of the week."""
    try:
        return await schedule_service.get_schedules_by_day(pool, day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener horarios por día: {e}")


@router.get("/stats/all", response_model=AllScheduleStatsResponse)
async def get_all_stats(
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get work hour usage statistics for all devices."""
    try:
        return await schedule_service.get_all_stats(pool)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener estadísticas: {e}")


@router.get("/stats/{device_id}", response_model=SingleScheduleStatsResponse)
async def get_device_stats(
    device_id: int,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get work hour usage statistics for a specific device."""
    try:
        return await schedule_service.get_device_stats(pool, device_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener estadísticas: {e}")


@router.get("/special-days/{device_id}", response_model=Dict[str, Any])
async def get_special_days(
    device_id: int,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get special days for a device."""
    try:
        return await schedule_service.get_special_days(pool, device_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener días especiales: {e}")


@router.post("/special-days/{device_id}", response_model=ScheduleRead)
async def add_special_day(
    device_id: int,
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    special_day: SpecialDaySchema = Body(...),
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Add or update a single special day for a device."""
    try:
        return await schedule_service.add_special_day(pool, device_id, date, special_day)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al añadir día especial: {e}")


@router.delete("/special-days/{device_id}/{date}", response_model=ScheduleDeleteResponse)
async def delete_special_day(
    device_id: int,
    date: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Delete a specific special day for a device."""
    try:
        return await schedule_service.delete_special_day(pool, device_id, date)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar día especial: {e}")


@router.get("/effective-schedule/{device_id}/{date}", response_model=Optional[DayScheduleSchema])
async def get_effective_schedule(
    device_id: int,
    date: str,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get the effective schedule for a device on a specific date."""
    try:
        return await schedule_service.get_effective_schedule(pool, device_id, date)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener horario efectivo: {e}")


@router.get("/", response_model=List[ScheduleRead])
async def get_all_schedules(
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get all schedules."""
    try:
        return await schedule_service.get_all_schedules(pool)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener los horarios: {e}")


@router.get("/{device_id}", response_model=Optional[ScheduleRead])
async def get_schedule(
    device_id: int,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Get the schedule for a specific device."""
    try:
        return await schedule_service.get_schedule(pool, device_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener el horario: {e}")


@router.delete("/{device_id}", response_model=ScheduleDeleteResponse)
async def delete_schedule(
    device_id: int,
    pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
):
    """Delete the schedule for a device."""
    try:
        await schedule_service.delete_schedule(pool, device_id)
        return ScheduleDeleteResponse(
            message=f"Horario del dispositivo {device_id} eliminado correctamente"
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar el horario: {e}")
