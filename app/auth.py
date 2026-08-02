from fastapi import Header, HTTPException

from app.config import AUTH_TOKEN


def error_response(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise error_response(401, "unauthorized", "Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ")
    if token != AUTH_TOKEN:
        raise error_response(401, "unauthorized", "Invalid bearer token")
