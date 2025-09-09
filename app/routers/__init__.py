"""
Routers module initialization.

This module exports all available routers for the FastAPI application.
"""

from .schedules import router as schedules_router

__all__ = ["schedules_router"]