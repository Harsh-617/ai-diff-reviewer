from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.db import init_db
from app.routes import health, spec
from app.routes.v1 import router as v1_router

app = FastAPI()

app.include_router(health.router)
app.include_router(spec.router)
app.include_router(v1_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
