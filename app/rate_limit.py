import asyncio
import math
import time
from collections import deque

from fastapi.responses import JSONResponse

from app.config import RATE_LIMIT_PER_MINUTE

WINDOW_SECONDS = 60.0

_timestamps: deque[float] = deque()
_lock = asyncio.Lock()


def _clock() -> float:
    return time.monotonic()


async def check_rate_limit() -> JSONResponse | None:
    """Sliding-window limiter for POST /v1/reviews. Returns a 429 JSONResponse
    when the limit is tripped, otherwise records the request and returns None.
    Never raises -- any unexpected error fails open so the limiter can't take
    the endpoint down.
    """
    try:
        async with _lock:
            now = _clock()
            cutoff = now - WINDOW_SECONDS
            while _timestamps and _timestamps[0] <= cutoff:
                _timestamps.popleft()

            if len(_timestamps) >= RATE_LIMIT_PER_MINUTE:
                retry_after = max(1, math.ceil(_timestamps[0] + WINDOW_SECONDS - now))
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": "Too many review requests; please retry later",
                        }
                    },
                    headers={"Retry-After": str(retry_after)},
                )

            _timestamps.append(now)
            return None
    except Exception:
        return None


def reset() -> None:
    global _lock
    _timestamps.clear()
    _lock = asyncio.Lock()
