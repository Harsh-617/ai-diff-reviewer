from fastapi import APIRouter, Depends

from app.auth import require_auth

router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
