"""
FastAPI dependencies for authentication and database access.
"""

import asyncpg
from fastapi import Header, HTTPException

from .config import settings
from .postgres import get_postgres


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    Verify the API key provided in the X-API-Key header.

    Raises:
        HTTPException: If the API key is invalid or not configured
    """
    if not settings.API_KEY:
        raise HTTPException(
            status_code=500, detail="API key no configurada en el servidor"
        )

    if x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=401,
            detail="API key inválida",
            headers={"WWW-Authenticate": "ApiKey"},
        )


async def get_db_pool() -> asyncpg.Pool:
    """
    FastAPI dependency that provides the database connection pool.
    """
    return await get_postgres()
