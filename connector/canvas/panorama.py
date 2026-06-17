"""Conversion-quality scoring and alternative-format resolution.

Every file Reflow processes ends up with:

  * a numeric **conversion quality** score (0..100) -- a heuristic
    that estimates how well the pipeline extracted accessible
    structure from the source PDF. This is **not** a WCAG conformance
    score. A separate publication gate runs WCAG checks and human
    review before content is approved for student-facing use.
  * a severity bucket (red / amber / green / dark green) for the gauge,
  * a list of available alternative formats,
  * a ranked list of issues for the instructor remediation popover.

This module is the single source of truth for those mappings. The
weights live here (not at call sites) so a future per-institution config
can override them without touching every endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["red", "amber", "green", "dark-green"]


@dataclass(frozen=True)
class SignalWeights:
    """Weights applied to each accessibility signal. Sum should be 100."""

    has_text_layer: int = 30
    headings_present: int = 20
    images_have_alt: int = 20
    tables_are_semantic: int = 15
    reading_order_linear: int = 10
    language_set: int = 5


@dataclass
class Score:
    # ``score`` and ``severity`` are None when ``status="unscanned"`` — i.e.
    # when Reflow returned no accessibility signals at all. Pretending we
    # know the score (the legacy default was 15/red regardless of input)
    # was misleading faculty into believing every document was scored.
    score: int | None  # 0..100, or None when unscanned
    severity: Severity | None
    available_formats: list[str] = field(default_factory=list)
    status: Literal["scored", "unscanned"] = "scored"


@dataclass
class Issue:
    code: str
    title: str
    severity: Severity
    page: int | None
    explanation: str
    fix_hint: str


# MVP default. Phase 7 lets institutions override.
DEFAULT_WEIGHTS = SignalWeights()

# Formats always available once Reflow has completed. Heavier formats
# (tagged PDF, ePub, MP3) come online in later phases — the API surface
# reports them only when the generator is wired up.
BASE_FORMATS = ["html", "txt", "markdown"]


def score_from_reflow_result(
    result: dict[str, Any],
    weights: SignalWeights = DEFAULT_WEIGHTS,
) -> Score:
    """Compose a score from a Reflow result payload.

    The Reflow API surfaces structured signals at the top level of the
    result document (``has_text_layer``, ``heading_levels``, etc.). When a
    signal is missing we treat it as failing — better to over-flag than
    silently award points.

    BUT: when *no* signals are present at all (empty / fallback payload),
    we return ``status="unscanned"`` instead of pretending we computed a
    score. Awarding the table-semantics-by-default points (the only
    branch that fires on empty input) made every document show as
    ``15/red``, which faculty correctly read as "scoring is broken".
    """

    # If Reflow didn't surface any of the signal keys we care about,
    # don't fabricate a score. Show an "unscanned" affordance instead.
    _signal_keys = (
        "has_text_layer", "heading_levels",
        "images_with_alt", "images_total",
        "tables_semantic", "tables_total",
        "reading_order_linear", "language",
    )
    if not any(k in result for k in _signal_keys):
        return Score(
            score=None,
            severity=None,
            available_formats=list(BASE_FORMATS),
            status="unscanned",
        )

    signals = {
        "has_text_layer": bool(result.get("has_text_layer", False)),
        "headings_present": bool(result.get("heading_levels", [])),
        "images_have_alt": _ratio(
            result.get("images_with_alt", 0), result.get("images_total", 0)
        )
        >= 0.95,
        "tables_are_semantic": _ratio(
            result.get("tables_semantic", 0), result.get("tables_total", 0)
        )
        >= 0.95
        if (result.get("tables_total", 0) or 0) > 0
        else True,
        "reading_order_linear": bool(result.get("reading_order_linear", False)),
        "language_set": bool(result.get("language")),
    }

    total = 0
    if signals["has_text_layer"]:
        total += weights.has_text_layer
    if signals["headings_present"]:
        total += weights.headings_present
    if signals["images_have_alt"]:
        total += weights.images_have_alt
    if signals["tables_are_semantic"]:
        total += weights.tables_are_semantic
    if signals["reading_order_linear"]:
        total += weights.reading_order_linear
    if signals["language_set"]:
        total += weights.language_set

    severity = severity_of(total)
    return Score(
        score=total,
        severity=severity,
        available_formats=list(BASE_FORMATS),
        status="scored",
    )


def severity_of(score: int) -> Severity:
    if score >= 90:
        return "dark-green"
    if score >= 67:
        return "green"
    if score >= 33:
        return "amber"
    return "red"


def source_accessibility_estimate(
    signals: dict[str, Any] | None,
) -> tuple[int | None, Severity | None]:
    """Rough estimate of the ORIGINAL PDF's accessibility — the "before".

    We can't run a PDF/UA conformance checker against the source here, so we
    infer a coarse number from the pipeline's classification of the source
    document (stored on ``job.signals`` at conversion time):

      * Scanned / image-only / no real text layer (or OCR had to be run):
        screen readers get nothing from the original — essentially
        unreadable. Returns a low "red" estimate (~12).
      * Born-digital text PDF: the text is selectable, but academic source
        PDFs are overwhelmingly *untagged* — no heading, list, table, or
        reading-order structure for assistive tech to navigate. Returns a
        low-moderate "amber" estimate (~45).

    This is deliberately a *before* number that sets up the before→after
    story against the WCAG score of the generated accessible version. It is
    an estimate, not a measurement — hence the conservative buckets.

    Returns ``(None, None)`` when no signals are available (e.g. a job that
    never reached completion), so the caller can omit the "before" dial
    rather than fabricate one.
    """
    if not signals:
        return None, None
    classification = str(signals.get("_pdf_classification") or "").lower()
    ocr_was_run = bool(signals.get("_ocr_was_run"))
    has_text_layer = bool(signals.get("has_text_layer"))
    scanned = (
        ocr_was_run
        or not has_text_layer
        or classification in {"scanned", "no_text", "image_only", "image-only"}
    )
    if scanned:
        return 12, severity_of(12)
    # Born-digital but (almost certainly) untagged.
    return 45, severity_of(45)


def issues_from_reflow_result(result: dict[str, Any]) -> list[Issue]:
    """Translate failing signals into instructor-facing issues."""

    issues: list[Issue] = []
    if not result.get("has_text_layer", False):
        issues.append(
            Issue(
                code="no-text-layer",
                title="This PDF is a scanned image, not real text",
                severity="red",
                page=None,
                explanation=(
                    "Screen readers cannot read this document because it does not "
                    "contain a text layer. Reflow recovered the text via OCR, but "
                    "the original PDF should be re-exported from the source."
                ),
                fix_hint=(
                    "Re-export the PDF from Word, PowerPoint, or the original source "
                    "rather than scanning a printed copy. If the original is gone, "
                    "use OCR software (e.g., Adobe Acrobat) and re-upload."
                ),
            )
        )
    if not result.get("heading_levels"):
        issues.append(
            Issue(
                code="missing-headings",
                title="No headings detected",
                severity="amber",
                page=None,
                explanation=(
                    "Headings let students with screen readers skim the document. "
                    "Without them the entire document reads as one long paragraph."
                ),
                fix_hint=(
                    "In the source document, mark section titles with Heading 1, 2, 3 "
                    "styles before exporting to PDF."
                ),
            )
        )
    missing_alt = (result.get("images_total", 0) or 0) - (
        result.get("images_with_alt", 0) or 0
    )
    if missing_alt > 0:
        issues.append(
            Issue(
                code="missing-alt",
                title=f"{missing_alt} image(s) without alt text",
                severity="amber",
                page=None,
                explanation=(
                    "Images without alternative text are invisible to screen readers. "
                    "Reflow proposed alt text where possible — review and edit it."
                ),
                fix_hint=(
                    "Open the accessible HTML version, edit the alt text for each "
                    "image, then approve and publish."
                ),
            )
        )
    if not result.get("language"):
        issues.append(
            Issue(
                code="no-language",
                title="Document language is not set",
                severity="green",
                page=None,
                explanation=(
                    "Setting the document language helps screen readers pick the "
                    "right pronunciation and helps translation tools."
                ),
                fix_hint="Set the document language in the source application's properties.",
            )
        )
    return issues


def _ratio(numerator: Any, denominator: Any) -> float:
    n = float(numerator or 0)
    d = float(denominator or 0)
    if d <= 0:
        return 0.0
    return n / d
