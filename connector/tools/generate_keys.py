"""Generate the secrets the connector expects in production.

Usage::

    python -m connector.tools.generate_keys

Prints a block of ``KEY=value`` lines suitable to paste into ``.env``.
Each value is a fresh 32-byte urlsafe token from ``secrets.token_urlsafe``
— enough entropy that brute-forcing the AES-GCM key derived from it via
SHA-256 is infeasible. The script does NOT touch ``.env`` itself; the
operator decides whether to overwrite or merge.

The keys printed:

* ``TOKEN_ENCRYPTION_KEY`` — derives the AES-GCM key that protects
  instructor OAuth tokens (and any other secret stored via
  ``privacy.encrypt_secret``). The most directly exploitable secret to
  miss in production.

* ``CSRF_SECRET_KEY`` — HMAC key for CSRF tokens on the state-changing
  POST endpoints (approve, reject, edit, pii-decision, unpublish). Also
  used as a fallback derivation source for ``TOKEN_ENCRYPTION_KEY`` when
  the explicit key isn't set.

Both can be rotated independently. Rotating ``TOKEN_ENCRYPTION_KEY``
makes existing encrypted tokens undecryptable — faculty must re-consent.
Rotating ``CSRF_SECRET_KEY`` invalidates outstanding CSRF tokens —
expected; clients re-fetch on next state-changing call.
"""

from __future__ import annotations

import secrets
import sys


def main() -> int:
    token_key = secrets.token_urlsafe(32)
    csrf_key = secrets.token_urlsafe(32)
    print(
        "# Paste these into .env. Rotating either key has consequences — "
        "see connector/tools/generate_keys.py docstring before rotating in prod."
    )
    print(f"TOKEN_ENCRYPTION_KEY={token_key}")
    print(f"CSRF_SECRET_KEY={csrf_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
