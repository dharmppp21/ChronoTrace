"""The API router: the three route groups combined into one, for the app to include."""

from fastapi import APIRouter

from chronotrace.server.routes import query, sessions, state

router = APIRouter()
router.include_router(sessions.router)
router.include_router(state.router)
router.include_router(query.router)

__all__ = ["router"]
