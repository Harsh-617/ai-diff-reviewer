from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.routes.v1 import reviews

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
router.include_router(reviews.router)
