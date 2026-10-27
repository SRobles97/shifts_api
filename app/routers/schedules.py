"""
Schedule API router — thin controllers delegating to the service layer.
"""

from datetime import date
from typing import Annotated, Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

from ..core.authz import (
    Editor,
    assert_can_edit_device,
    assert_can_edit_devices,
    require_schedule_editor,
)
from ..core.dependencies import get_db_pool, verify_api_key
from ..schemas.day_changes import (
    BulkScheduleRequest,
    BulkScheduleResponse,
    DayChangeRequest,
    DayChangeResponse,
    EffectiveBlock,
    ExceptionRead,
)
from ..schemas.schedule import (
    AllScheduleStatsResponse,
    DayScheduleSchema,
    ScheduleCreate,
    ScheduleDeleteResponse,
    SchedulePatch,
    ScheduleRead,
    ScheduleUpdate,
    SingleScheduleStatsResponse,
    SpecialDaySchema,
)
from ..services import bulk_schedule_service, day_change_service
from ..services.schedule_service import MirroredScheduleError, schedule_service

router = APIRouter(prefix="/schedules", tags=["schedules"])

# ── Reusable Annotated types ──────────────────────────────────────────

Pool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
ApiKey = Annotated[None, Depends(verify_api_key)]
# Verified caller (bearer -> auth_api /auth/me) for the write routes; fails
# closed — see app/core/authz.py.
ScheduleEditor = Annotated[Editor, Depends(require_schedule_editor)]

DATE_QUERY_DESC = "Target date (YYYY-MM-DD) to resolve schedule"
DateQuery = Annotated[Optional[date], Query(alias="date", description=DATE_QUERY_DESC)]

SHIFT_TYPE_DESC = "Shift type: 'day' or 'night' (defaults to 'day')"
ShiftTypeQuery = Annotated[str, Query(alias="shiftType", description=SHIFT_TYPE_DESC)]
OptionalShiftTypeQuery = Annotated[Optional[str], Query(alias="shiftType", description="Filter by shift type: 'day' or 'night'. Omit to get all.")]

# ── Reusable response docs ────────────────────────────────────────────

_404 = {404: {"description": "Resource not found"}}
_400 = {400: {"description": "Bad request / validation error"}}
_500 = {500: {"description": "Internal server error"}}

RESPONSES_400_500 = {**_400, **_500}
RESPONSES_404_500 = {**_404, **_500}
RESPONSES_404_400_500 = {**_404, **_400, **_500}
RESPONSES_500 = {**_500}


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


@router.post("/", response_model=ScheduleRead, responses=RESPONSES_404_400_500)
async def create_schedule(
    data: ScheduleCreate,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Create a schedule for a device (auto-closes previous open-ended schedule)."""
    try:
        device_id = await schedule_service.resolve_device_id(pool, data)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await assert_can_edit_device(pool, editor, device_id)
    try:
        return await schedule_service.create_schedule(pool, data, actor=editor.actor())
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear el horario: {e}")


# ========== Day Changes ("Cambiar un día") ==========
# Static paths: declared before the /{device_id} routes.


@router.post("/day-changes/preview", response_model=DayChangeResponse, responses=RESPONSES_404_400_500)
async def preview_day_change(
    data: DayChangeRequest,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Preview a per-date change over several machines; nothing is written."""
    refs = await assert_can_edit_devices(pool, editor, data.device_ids)
    try:
        return await day_change_service.preview_day_change(pool, data, refs)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al previsualizar el cambio del día: {e}")


@router.post("/day-changes", response_model=DayChangeResponse, responses=RESPONSES_404_400_500)
async def apply_day_change(
    data: DayChangeRequest,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Apply a per-date change to several machines at once (one email)."""
    refs = await assert_can_edit_devices(pool, editor, data.device_ids)
    try:
        return await day_change_service.apply_day_change(pool, data, refs, editor.actor())
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al aplicar el cambio del día: {e}")


@router.get("/exceptions", response_model=List[ExceptionRead], response_model_by_alias=True, responses=RESPONSES_400_500)
async def list_exceptions(
    pool: Pool,
    _: ApiKey,
    device_ids: Annotated[str, Query(alias="deviceIds", description="Comma-separated device ids")],
    range_from: Annotated[date, Query(alias="from", description="Start date (YYYY-MM-DD)")],
    range_to: Annotated[date, Query(alias="to", description="End date (YYYY-MM-DD), at most 400 days after from")],
):
    """List per-date exceptions of several machines in a date range."""
    try:
        ids = [int(x) for x in device_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="deviceIds debe ser una lista de enteros separados por coma")
    try:
        return await day_change_service.list_exceptions(pool, ids, range_from, range_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al listar las excepciones: {e}")


@router.get(
    "/effective-blocks",
    response_model=List[EffectiveBlock],
    response_model_by_alias=True,
    responses=RESPONSES_400_500,
)
async def get_effective_blocks(
    pool: Pool,
    _: ApiKey,
    device_ids: Annotated[str, Query(alias="deviceIds", description="Comma-separated device ids")],
    range_from: Annotated[date, Query(alias="from", description="Start date (YYYY-MM-DD)")],
    range_to: Annotated[date, Query(alias="to", description="End date (YYYY-MM-DD), at most 400 days in total")],
):
    """Work blocks of several machines in a date range, with exceptions and overtime applied."""
    try:
        ids = [int(x) for x in device_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="deviceIds debe ser una lista de enteros separados por coma")
    try:
        return await schedule_service.get_effective_blocks(pool, ids, range_from, range_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener los bloques efectivos: {e}")


@router.delete("/exceptions/{exception_id}", response_model=ScheduleDeleteResponse, responses=RESPONSES_404_500)
async def remove_exception(
    exception_id: int,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Remove one per-date exception (one email)."""
    try:
        await day_change_service.remove_exception(pool, exception_id, editor)
        return ScheduleDeleteResponse(message=f"Excepción {exception_id} eliminada correctamente")
    except HTTPException:
        raise
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar la excepción: {e}")


# ========== Bulk regular schedule ("Editar turno habitual") ==========
# Static paths: declared before the /{device_id} routes.


@router.post("/bulk/preview", response_model=BulkScheduleResponse, responses=RESPONSES_404_400_500)
async def preview_bulk_schedule(
    data: BulkScheduleRequest,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Preview a new regular schedule from a date over several machines; nothing is written."""
    refs = await assert_can_edit_devices(pool, editor, data.device_ids)
    try:
        return await bulk_schedule_service.preview_bulk(pool, data, refs)
    except HTTPException:
        raise
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al previsualizar el turno habitual: {e}")


@router.post("/bulk", response_model=BulkScheduleResponse, responses=RESPONSES_404_400_500)
async def apply_bulk_schedule(
    data: BulkScheduleRequest,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
):
    """Apply a new regular schedule from a date to several machines at once (one email)."""
    refs = await assert_can_edit_devices(pool, editor, data.device_ids)
    try:
        return await bulk_schedule_service.apply_bulk(pool, data, refs, editor.actor())
    except HTTPException:
        raise
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al aplicar el turno habitual: {e}")


@router.put("/{device_id}", response_model=ScheduleRead, responses=RESPONSES_404_400_500)
async def update_schedule(
    device_id: int,
    data: ScheduleUpdate,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
    date_param: DateQuery = None,
    shift_type: ShiftTypeQuery = "day",
):
    """Full replacement of a schedule for a device."""
    await assert_can_edit_device(pool, editor, device_id)
    try:
        return await schedule_service.update_schedule(pool, device_id, data, target_date=date_param, shift_type=shift_type, actor=editor.actor())
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar el horario: {e}")


@router.patch("/{device_id}", response_model=ScheduleRead, responses=RESPONSES_404_400_500)
async def patch_schedule(
    device_id: int,
    data: SchedulePatch,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
    date_param: DateQuery = None,
    shift_type: ShiftTypeQuery = "day",
):
    """Partial update of a schedule for a device."""
    await assert_can_edit_device(pool, editor, device_id)
    try:
        return await schedule_service.patch_schedule(pool, device_id, data, target_date=date_param, shift_type=shift_type, actor=editor.actor())
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al actualizar parcialmente: {e}")


@router.get("/by-day/{day}", response_model=List[ScheduleRead], responses=RESPONSES_400_500)
async def get_schedules_by_day(
    day: str,
    pool: Pool,
    _: ApiKey,
):
    """Get all currently effective schedules active on a specific day of the week."""
    try:
        return await schedule_service.get_schedules_by_day(pool, day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener horarios por día: {e}")


@router.get("/stats/all", response_model=AllScheduleStatsResponse, responses=RESPONSES_500)
async def get_all_stats(
    pool: Pool,
    _: ApiKey,
):
    """Get work hour usage statistics for all devices."""
    try:
        return await schedule_service.get_all_stats(pool)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener estadísticas: {e}")


@router.get("/stats/{device_id}", response_model=SingleScheduleStatsResponse, responses=RESPONSES_404_500)
async def get_device_stats(
    device_id: int,
    pool: Pool,
    _: ApiKey,
    shift_type: ShiftTypeQuery = "day",
):
    """Get work hour usage statistics for a specific device."""
    try:
        return await schedule_service.get_device_stats(pool, device_id, shift_type=shift_type)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener estadísticas: {e}")


@router.get("/special-days/{device_id}", response_model=Dict[str, Any], responses=RESPONSES_404_500)
async def get_special_days(
    device_id: int,
    pool: Pool,
    _: ApiKey,
    shift_type: ShiftTypeQuery = "day",
):
    """Get special days for a device."""
    try:
        return await schedule_service.get_special_days(pool, device_id, shift_type=shift_type)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener días especiales: {e}")


@router.post("/special-days/{device_id}", response_model=ScheduleRead, responses=RESPONSES_404_400_500)
async def add_special_day(
    device_id: int,
    date: Annotated[str, Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")],
    special_day: Annotated[SpecialDaySchema, Body(...)],
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
    shift_type: ShiftTypeQuery = "day",
):
    """Add or update a single special day for a device."""
    await assert_can_edit_device(pool, editor, device_id)
    try:
        return await schedule_service.add_special_day(pool, device_id, date, special_day, shift_type=shift_type)
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al añadir día especial: {e}")


@router.delete("/special-days/{device_id}/{date}", response_model=ScheduleDeleteResponse, responses=RESPONSES_404_400_500)
async def delete_special_day(
    device_id: int,
    date: str,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
    shift_type: ShiftTypeQuery = "day",
):
    """Delete a specific special day for a device."""
    await assert_can_edit_device(pool, editor, device_id)
    try:
        return await schedule_service.delete_special_day(
            pool, device_id, date, shift_type=shift_type, editor=editor
        )
    except HTTPException:
        raise
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar día especial: {e}")


@router.get("/effective-schedule/{device_id}/{date}", response_model=Optional[DayScheduleSchema], responses=RESPONSES_404_400_500)
async def get_effective_schedule(
    device_id: int,
    date: str,
    pool: Pool,
    _: ApiKey,
    shift_type: ShiftTypeQuery = "day",
):
    """Get the effective schedule for a device on a specific date.

    Obsoleto: no conoce las excepciones por fecha ni las horas extras, y su forma
    (un solo tramo) no las puede representar. Usar /effective-blocks.
    """
    try:
        return await schedule_service.get_effective_schedule(pool, device_id, date, shift_type=shift_type)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener horario efectivo: {e}")


@router.get("/", response_model=List[ScheduleRead], responses=RESPONSES_500)
async def get_all_schedules(
    pool: Pool,
    _: ApiKey,
    range_from: Optional[date] = Query(None, alias="from", description="Start of date range (YYYY-MM-DD)"),
    range_to: Optional[date] = Query(None, alias="to", description="End of date range (YYYY-MM-DD)"),
):
    """Get all schedules. With from/to params, returns all schedules overlapping that range. Without, returns currently effective only."""
    try:
        return await schedule_service.get_all_schedules(pool, range_from, range_to)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener los horarios: {e}")


@router.get("/{device_id}/history", response_model=List[ScheduleRead], responses=RESPONSES_500)
async def get_schedule_history(
    device_id: int,
    pool: Pool,
    _: ApiKey,
):
    """Get all schedules (history) for a specific device."""
    try:
        return await schedule_service.get_schedule_history(pool, device_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener historial: {e}")


@router.get("/{device_id}", response_model=List[ScheduleRead], responses=RESPONSES_500)
async def get_schedule(
    device_id: int,
    pool: Pool,
    _: ApiKey,
    date_param: DateQuery = None,
    shift_type: OptionalShiftTypeQuery = None,
):
    """Get schedules for a device. Returns all shift types when shiftType is omitted."""
    try:
        return await schedule_service.get_device_schedules(pool, device_id, target_date=date_param, shift_type=shift_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener el horario: {e}")


@router.delete("/{device_id}", response_model=ScheduleDeleteResponse, responses=RESPONSES_404_500)
async def delete_schedule(
    device_id: int,
    pool: Pool,
    _: ApiKey,
    editor: ScheduleEditor,
    schedule_id: Annotated[Optional[int], Query(alias="scheduleId", description="Specific schedule ID to delete")] = None,
    shift_type: ShiftTypeQuery = "day",
):
    """Delete a schedule for a device (current or by specific schedule ID)."""
    await assert_can_edit_device(pool, editor, device_id)
    try:
        await schedule_service.delete_schedule(pool, device_id, schedule_id=schedule_id, shift_type=shift_type, actor=editor.actor())
        return ScheduleDeleteResponse(
            message=f"Horario del dispositivo {device_id} eliminado correctamente"
        )
    except MirroredScheduleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar el horario: {e}")
