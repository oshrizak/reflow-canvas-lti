"""Validate environment configuration before boot.

Run via ``python scripts/preflight.py``. Intended to surface missing or
inconsistent settings early — before the connector tries to talk to Canvas
or Reflow Core.

Fleshed out in Phase B once connector/config.py exists.
"""
from __future__ import annotations

import sys


def main() -> int:
    print("preflight: not yet implemented — see Phase B in PORTING_BRIEF.md", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
