"""Smoke tests: the connector package imports and exposes the routers we expect.

Intentionally minimal — Phase H proved end-to-end behaviour against a live
Reflow Core. These checks catch wiring regressions (a router accidentally
dropped from ``main.py``, an import-time failure in any of the ported
modules) and give CI a non-empty test suite so it doesn't fail with
"no tests collected".
"""

import pytest


@pytest.mark.unit
def test_main_imports() -> None:
    """The whole module graph imports clean — exercises every connector subpkg."""
    import connector.main  # noqa: F401

    assert connector.main.app.title == "Reflow Canvas LTI Connector"


@pytest.mark.unit
def test_expected_routes_mounted() -> None:
    """Every router the brief promises is wired into the FastAPI app.

    Enumerates via ``app.openapi()`` rather than ``app.routes`` because
    FastAPI 0.137+ stores ``include_router`` results as ``_IncludedRouter``
    placeholders in ``app.routes`` until OpenAPI generation expands them.
    """
    from connector.main import app

    paths = set(app.openapi()["paths"].keys())

    # App-level health probe
    assert "/health" in paths

    # LTI handshake — only endpoints that are surfaced in OpenAPI
    # (``/panorama.js`` is excluded with include_in_schema=False).
    assert "/lti/config.json" in paths
    assert "/lti/jwks" in paths
    assert "/lti/login" in paths
    assert "/lti/launch" in paths

    # Each Canvas API router contributes >=1 path under its prefix.
    for prefix in ("/canvas/consent", "/canvas/oauth", "/canvas/panorama", "/canvas/review"):
        assert any(p.startswith(prefix) for p in paths), (
            f"No routes mounted under {prefix}"
        )
