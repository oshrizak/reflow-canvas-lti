"""LTI 1.3 integration for Canvas LMS.

This package exposes endpoints Canvas calls during the LTI launch dance
(OIDC login, signed launch JWT, JWKS, tool configuration) and the helpers
that validate Canvas-issued JWTs against the platform's JWKS.

The mounted router is wired into the FastAPI app in ``connector.main``.
"""

from .routes import router

__all__ = ["router"]
