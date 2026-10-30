"""Admin-only API surface (Milestone 5 foundation).

Every admin route must be registered on ``router`` (or another router built
by ``admin_router``) so the ``require_admin`` guard applies to it without
relying on each endpoint remembering a dependency. The router has no
endpoints yet and is deliberately NOT included in ``main.app``: the first
real admin endpoint (verification queue, suspension, reports) and its
``app.include_router`` line land together in a later PR.
``tests/test_admin_guard.py`` fails if any ``/admin`` route on the app is
reachable without ``require_admin``.
"""

from fastapi import APIRouter, Depends

from routers.dependencies import require_admin

ADMIN_PREFIX = "/admin"


def admin_router(prefix: str = "", **kwargs) -> APIRouter:
    """Build a router whose every route requires an authenticated admin."""
    dependencies = [Depends(require_admin), *kwargs.pop("dependencies", [])]
    return APIRouter(
        prefix=f"{ADMIN_PREFIX}{prefix}",
        dependencies=dependencies,
        tags=kwargs.pop("tags", ["Admin"]),
        **kwargs,
    )


router = admin_router()
