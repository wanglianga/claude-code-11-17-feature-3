"""pytest 夹具：独立测试库 qc_lab_test，每个测试函数重建表，互不污染。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/qc_lab_test"
)
os.environ.setdefault("INSPECTOR_TOKEN", "token-inspector-001")
os.environ.setdefault("REVIEWER_TOKEN", "token-reviewer-001")
os.environ.setdefault("ADMIN_TOKEN", "token-admin-001")

from app import models  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_all  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

TOKENS = {
    "inspector": "token-inspector-001",
    "reviewer": "token-reviewer-001",
    "admin": "token-admin-001",
}


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/qc_lab_test",
        pool_pre_ping=True,
        future=True,
    )
    yield eng
    eng.dispose()


@pytest.fixture()
def db_session(engine):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSession()

    def override_get_db():
        try:
            yield session
        finally:
            pass  # 由测试统一控制提交/回滚

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()


@pytest.fixture()
def client(db_session):
    return TestClient(app)


@pytest.fixture()
def seeded(db_session):
    result = seed_all(db_session, DATA_DIR / "reference")
    db_session.commit()
    return result


def auth_header(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}
