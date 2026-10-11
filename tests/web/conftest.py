import pytest
from fastapi.testclient import TestClient

from mcdash.web.app import create_app

from .fakes import FakeDashboardService, seeded

ACTOR = "noah@example.com"
HX = {"HX-Request": "true"}


@pytest.fixture
def svc() -> FakeDashboardService:
    return seeded()


@pytest.fixture
def client(svc: FakeDashboardService) -> TestClient:
    return TestClient(
        create_app(svc),
        headers={"X-Auth-Request-Email": ACTOR},
        follow_redirects=False,
    )
