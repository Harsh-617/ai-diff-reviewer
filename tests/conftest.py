import uuid

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.config import AUTH_TOKEN
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    monkeypatch.setattr(db_module, "DATABASE_PATH", str(db_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers():
    return {"Authorization": f"Bearer {AUTH_TOKEN}"}
