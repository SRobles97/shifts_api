"""
Main FastAPI application module.

This module initializes the FastAPI application with proper middleware,
database connections, and routing configuration.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.logging import setup_logging
from app.core.middleware import LoggingMiddleware
from app.core.postgres import init_db, close_postgres
from app.routers.schedules import router as schedules_router

# Configure logging before creating the app
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    
    Handles startup and shutdown events for the FastAPI application.
    """
    # Startup
    try:
        await init_db()
        logger.info("Database initialized successfully")
        logger.info("API started successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        logger.warning("API starting without database connection - database endpoints will not work")
        # Don't raise - allow API to start without database for testing
    
    yield
    
    # Shutdown
    try:
        await close_postgres()
        logger.info("Database connections closed")
    except Exception as e:
        logger.error(f"Error closing database connections: {e}")
    
    logger.info("API stopped")


app = FastAPI(
    title="Shifts API - Schedule Management",
    description="API for managing work schedules and shifts for devices",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

# Add logging middleware
app.add_middleware(LoggingMiddleware)

# Include routers
app.include_router(schedules_router, prefix="/shifts-api/v1")


@app.get("/shifts-api")
async def root():
    """Health check endpoint."""
    logger.info("Health check endpoint accessed")
    return {
        "message": "Shifts API is running", 
        "status": "healthy",
        "version": "1.0.0"
    }


@app.get("/shifts-api/health")
async def health_check():
    """Detailed health check endpoint."""
    logger.info("Health check endpoint accessed")
    return {
        "status": "healthy",
        "service": "shifts-api",
        "version": "1.0.0",
        "endpoints": {
            "schedules": "/api/v1/schedules",
            "health": "/health",
            "docs": "/docs"
        }
    }