import sys
import os
from pathlib import Path
from loguru import logger
from typing import Optional

# Crear directorio de logs si no existe
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


def setup_logging(
    log_level: str = "INFO",
    enable_file_logging: bool = True,
    enable_console_logging: bool = True,
    log_rotation: str = "10 MB",
    log_retention: str = "30 days",
    json_format: bool = False,
):
    """
    Configura el sistema de logging con Loguru

    Args:
        log_level: Nivel de logging (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        enable_file_logging: Habilitar logging a archivo
        enable_console_logging: Habilitar logging en consola
        log_rotation: Rotación de archivos de log
        log_retention: Tiempo de retención de logs
        json_format: Usar formato JSON para los logs
    """

    # Remover el handler por defecto de loguru
    logger.remove()

    # Formato para logs de desarrollo (más legible)
    dev_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # Formato JSON para producción
    json_format_template = (
        "{{"
        '"time": "{time}", '
        '"level": "{level}", '
        '"module": "{name}", '
        '"function": "{function}", '
        '"line": {line}, '
        '"message": "{message}"'
        "}}"
    )

    format_template = json_format_template if json_format else dev_format

    # Configurar logging en consola
    if enable_console_logging:
        logger.add(
            sys.stdout,
            format=format_template,
            level=log_level,
            colorize=not json_format,
            backtrace=True,
            diagnose=True,
        )

    # Configurar logging en archivos
    if enable_file_logging:
        # Log general (INFO y superior)
        logger.add(
            LOG_DIR / "app_{time:YYYY-MM-DD}.log",
            format=format_template,
            level="INFO",
            rotation=log_rotation,
            retention=log_retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
        )

        # Log de errores (WARNING y superior)
        logger.add(
            LOG_DIR / "errors_{time:YYYY-MM-DD}.log",
            format=format_template,
            level="WARNING",
            rotation=log_rotation,
            retention=log_retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
        )

        # Log de debug (solo en desarrollo)
        if log_level == "DEBUG":
            logger.add(
                LOG_DIR / "debug_{time:YYYY-MM-DD}.log",
                format=format_template,
                level="DEBUG",
                rotation=log_rotation,
                retention=log_retention,
                encoding="utf-8",
                backtrace=True,
                diagnose=True,
            )


def get_logger(name: Optional[str] = None):
    """
    Obtiene un logger con el nombre especificado
    """
    if name:
        return logger.bind(name=name)
    return logger


# Configurar logging al importar el módulo
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
ENABLE_JSON_LOGS = os.getenv("ENABLE_JSON_LOGS", "false").lower() == "true"
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

setup_logging(
    log_level=LOG_LEVEL,
    json_format=ENABLE_JSON_LOGS,
    enable_console_logging=True,
    enable_file_logging=True,
)
