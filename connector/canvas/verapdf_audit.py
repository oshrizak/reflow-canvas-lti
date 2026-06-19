"""VeraPDF-backed accessibility audit for the source PDF.

The dial's ``source_score`` historically came from ``canvas.signals`` —
a heuristic over the converted markdown that the module itself flagged
as "NOT a WCAG conformance proof, just a conversion-quality signal".
This module replaces that with a real PDF/UA-1 audit run by
`VeraPDF <https://verapdf.org>`_, the PDF Association's reference
validator. Every failing assertion maps to a WCAG 2.x criterion; the
score is the proportion of applicable rules that passed.

The connector image installs VeraPDF (and a JRE) at build time — see
the ``ARG VERAPDF_VERSION`` block in ``Dockerfile``. Running the
``verapdf`` subprocess is bounded by ``DEFAULT_TIMEOUT_SECONDS`` so a
pathological PDF can't stall the watcher tick.

The audit is intentionally side-effect-free: it takes PDF bytes in,
writes them to a temp file, runs the validator, parses the JSON, and
returns a structured result. Whether to persist it (e.g. into the
``CanvasJob`` Redis hash) is the caller's choice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 90.0
"""Per-file ceiling on the verapdf subprocess. Most academic PDFs finish in
2-15s; a 90s budget gives headroom for large textbooks without letting one
runaway document stall the watcher."""

DEFAULT_FLAVOUR = "ua1"
"""``ua1`` is the PDF/UA-1 profile mapped to WCAG 2.x. Other VeraPDF
profiles (``1a``, ``1b``, ``2u``, etc.) target PDF/A archival compliance,
which isn't what accessibility scoring needs."""


class VeraPDFError(Exception):
    """Raised when the verapdf subprocess fails or returns no parseable output."""


@dataclass
class RuleViolation:
    """A single PDF/UA / WCAG criterion that failed validation."""

    rule_id: str
    """Identifier like ``7.1-1`` (clause + statement)."""
    clause: str
    """The PDF/UA clause that the rule lives under (e.g. ``7.1``)."""
    description: str
    """Human-readable explanation of what the rule checks."""
    severity: str
    """``error`` (must fix) or ``warning`` (should fix) — VeraPDF terminology."""
    occurrence_count: int
    """How many times this rule failed in the document."""


@dataclass
class AuditResult:
    """Outcome of a VeraPDF run against one PDF."""

    score: int
    """``0..100``. ``(passed_rules / applicable_rules) * 100`` rounded to int."""
    passed_rules: int
    failed_rules: int
    """How many distinct rules failed at least once."""
    total_occurrences: int
    """Sum of occurrence counts across all failed rules."""
    violations: list[RuleViolation] = field(default_factory=list)
    is_compliant: bool = False
    """True only when zero rules failed at error severity. PDF/UA conformance
    is binary; the percentage is a relative measure for triage, not certification."""
    flavour: str = DEFAULT_FLAVOUR
    raw_json: dict[str, Any] | None = None
    """The raw VeraPDF JSON output. Caller-discretion to log or persist."""


def _resolve_executable() -> str:
    """Locate the verapdf binary on PATH. Raises a clear error if missing."""
    exe = shutil.which("verapdf")
    if exe is None:
        raise VeraPDFError(
            "verapdf executable not found on PATH. The connector image installs "
            "it under /opt/verapdf; if you're running outside Docker, install "
            "from https://verapdf.org/software/."
        )
    return exe


async def run_audit(
    pdf_bytes: bytes,
    *,
    flavour: str = DEFAULT_FLAVOUR,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> AuditResult:
    """Validate a PDF against the chosen VeraPDF profile.

    Args:
        pdf_bytes: The PDF file's raw bytes. Written to a temp file because
            VeraPDF's CLI is path-oriented; the temp file is removed even on
            exception.
        flavour: VeraPDF profile id. ``ua1`` is PDF/UA-1 → WCAG mapping
            (default and what the dial wants).
        timeout_seconds: Hard timeout on the subprocess. On timeout the
            child is killed and a ``VeraPDFError`` raised.

    Returns:
        ``AuditResult`` with the score and structured rule violations.

    Raises:
        VeraPDFError: VeraPDF not installed, the subprocess errored,
            timed out, or produced unparseable output.
    """
    exe = _resolve_executable()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(pdf_bytes)
        fh.flush()
        pdf_path = Path(fh.name)

    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "--flavour", flavour,
            "--format", "json",
            str(pdf_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise VeraPDFError(
                f"verapdf timed out after {timeout_seconds:.0f}s on {pdf_path.name}"
            ) from None
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove verapdf temp file %s", pdf_path)

    if proc.returncode not in (0, 1):
        # 0 = compliant, 1 = non-compliant (both produce parseable JSON).
        # Anything else is a real failure (bad args, parse error, OOM, etc.).
        stderr = stderr_b.decode("utf-8", "replace").strip()
        raise VeraPDFError(
            f"verapdf exited {proc.returncode}: {stderr[:500] or 'no stderr'}"
        )

    stdout = stdout_b.decode("utf-8", "replace").strip()
    if not stdout:
        raise VeraPDFError("verapdf produced no stdout")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise VeraPDFError(
            f"verapdf stdout was not JSON: {stdout[:200]}"
        ) from exc

    return _parse_report(payload, flavour=flavour)


def _parse_report(payload: dict[str, Any], *, flavour: str) -> AuditResult:
    """Translate VeraPDF's JSON report into an ``AuditResult``.

    VeraPDF JSON shape (abridged)::

        {
          "report": {
            "jobs": [{
              "validationResult": {
                "compliant": false,
                "details": {
                  "passedRules": 24,
                  "failedRules": 5,
                  "failedChecks": 18,
                  "ruleSummaries": [
                    {
                      "ruleStatus": "FAILED",
                      "specification": "PDF/UA-1",
                      "clause": "7.1",
                      "testNumber": 1,
                      "description": "All real content must be marked …",
                      "object": "…",
                      "failedChecks": 4
                    },
                    …
                  ]
                }
              }
            }]
          }
        }
    """
    jobs = (
        payload.get("report", {})
        .get("jobs", [])
    )
    if not jobs:
        return AuditResult(
            score=0,
            passed_rules=0,
            failed_rules=0,
            total_occurrences=0,
            violations=[],
            is_compliant=False,
            flavour=flavour,
            raw_json=payload,
        )

    job = jobs[0]
    # validationResult shape changed in verapdf 1.30: it's now a list (one
    # entry per profile applied), where previously it was a single object.
    # Tolerate both. When the list is empty, fall back to a stub so the
    # details lookup below returns a clean zero-rules report.
    raw_validation = job.get("validationResult", {})
    if isinstance(raw_validation, list):
        validation = raw_validation[0] if raw_validation else {}
    else:
        validation = raw_validation or {}
    details = validation.get("details", {}) or {}

    passed = int(details.get("passedRules", 0) or 0)
    failed = int(details.get("failedRules", 0) or 0)
    total_applicable = passed + failed
    score = (
        int(round(100 * passed / total_applicable))
        if total_applicable > 0
        else 0
    )

    violations: list[RuleViolation] = []
    total_occurrences = 0
    for summary in details.get("ruleSummaries", []) or []:
        if summary.get("ruleStatus") != "FAILED":
            continue
        clause = str(summary.get("clause", "") or "")
        test_number = summary.get("testNumber")
        rule_id = f"{clause}-{test_number}" if test_number is not None else clause
        occ = int(summary.get("failedChecks", 0) or 0)
        total_occurrences += occ
        violations.append(
            RuleViolation(
                rule_id=rule_id,
                clause=clause,
                description=str(summary.get("description", "") or ""),
                severity="error",
                occurrence_count=occ,
            )
        )

    return AuditResult(
        score=score,
        passed_rules=passed,
        failed_rules=failed,
        total_occurrences=total_occurrences,
        violations=violations,
        is_compliant=bool(validation.get("compliant", False)),
        flavour=flavour,
        raw_json=payload,
    )
