import time
import uuid
from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware para logging automático de requests y responses"""

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # Generar ID único para el request
        request_id = str(uuid.uuid4())[:8]

        # Obtener información del request
        start_time = time.time()
        client_ip = request.client.host
        method = request.method
        url = str(request.url)
        user_agent = request.headers.get("user-agent", "")

        # Log del request entrante
        logger.info(
            f"Request started",
            extra={
                "request_id": request_id,
                "method": method,
                "url": url,
                "client_ip": client_ip,
                "user_agent": user_agent,
            },
        )

        # Procesar el request
        try:
            response: Response = await call_next(request)

            # Calcular tiempo de procesamiento
            process_time = time.time() - start_time

            # Log del response
            logger.info(
                f"Request completed",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "url": url,
                    "status_code": response.status_code,
                    "process_time": f"{process_time:.4f}s",
                },
            )

            # Agregar headers útiles
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time:.4f}"

            return response

        except Exception as e:
            # Log de errores
            process_time = time.time() - start_time
            logger.error(
                f"Request failed: {str(e)}",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "url": url,
                    "error": str(e),
                    "process_time": f"{process_time:.4f}s",
                },
            )
            raise
