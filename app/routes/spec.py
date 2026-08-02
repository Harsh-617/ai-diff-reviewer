from fastapi import APIRouter

from app.config import (
    CHUNK_BYTES,
    MAX_CONCURRENT_JOBS,
    MAX_PAYLOAD_BYTES,
    RATE_LIMIT_PER_MINUTE,
)
from app.models import Limits, SpecResponse

router = APIRouter()


@router.get("/spec", response_model=SpecResponse)
def get_spec() -> SpecResponse:
    return SpecResponse(
        specVersion="1.0",
        providers=["mock", "llm"],
        limits=Limits(
            maxPayloadBytes=MAX_PAYLOAD_BYTES,
            chunkBytes=CHUNK_BYTES,
            maxConcurrentJobs=MAX_CONCURRENT_JOBS,
            rateLimitPerMinute=RATE_LIMIT_PER_MINUTE,
        ),
    )
