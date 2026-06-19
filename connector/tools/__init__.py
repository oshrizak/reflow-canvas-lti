"""One-shot operational scripts (CLI entry points).

These modules are intentionally not imported by the runtime — they're
invoked via ``python -m connector.tools.<name>`` for backfills,
migrations, and one-off fixes. Keep them small and idempotent.
"""
