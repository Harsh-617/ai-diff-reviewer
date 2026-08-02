import time

from fastapi import APIRouter

from app.models import HealthResponse

router = APIRouter()

_start_time = time.monotonic()
VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse)
def get_health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=VERSION,
        uptimeSeconds=time.monotonic() - _start_time,
    )
