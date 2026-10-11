"""Test doubles for the web layer.

The fake itself lives in ``mcdash.web.fake`` so the dev server can use it too;
it is re-exported here for tests.
"""

from mcdash.models import DashboardService
from mcdash.web.fake import FakeDashboardService, seeded

__all__ = ["FakeDashboardService", "seeded", "check_protocol"]


def check_protocol(svc: FakeDashboardService) -> DashboardService:
    """Static check (pyright) that the fake satisfies the Protocol."""
    return svc
