"""
API Routers package.
"""

from doc_extractor.api.routers.health import router as health_router

# The API-key management router (API-019) is held back until decision M5-D08 (idempotent replay
# of a "shown once" secret) is approved; see the M5 integration plan.
__all__ = ["health_router"]
