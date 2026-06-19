"""Integration tests.

These exercise the real FastAPI handlers, a real (in-memory) Redis
via ``fakeredis``, and the real LTI-session + CSRF + rate-limiter
plumbing. Outbound HTTP (Reflow Core, Canvas) is mocked with
``respx`` so the suite runs hermetically.

Why these exist on top of the ``unit`` tests: the user-facing flows
we touched repeatedly (PII decision, approve+publish, rate limit
boundaries, the figure proxy) each break in a *different* layer from
the pure-logic tests — wrong URL, missing CSRF token, wrong Redis
key shape. The integration tier asserts the end-to-end contract so
we catch regressions in any of those layers.
"""
