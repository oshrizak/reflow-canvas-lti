"""Automated WCAG 2.1 AA-oriented checks on generated/edited HTML.

This is the *automated* half of the Phase 7 publication gate. It runs
deterministic structural checks against the HTML we're about to serve
and produces a list of failures the reviewer must either fix or
explicitly waive.

What this is NOT:
  * Not a substitute for human review. Things like meaningful alt
    text, accurate reading order on multi-column scans, and math
    equivalents require a human.
  * Not a substitute for a full axe-core run. axe-core covers ~30%
    of WCAG criteria automatically; this module is a focused subset
    of the most-common-failure structural rules. Adding axe-core
    via headless Chrome is a follow-up.

Each finding has:
  * ``rule_id``    -- stable identifier for filtering/waivers
  * ``severity``   -- "error" / "warning" / "info"
  * ``wcag_ref``   -- WCAG 2.1 success criterion (e.g. "1.3.1")
  * ``message``    -- human-readable
  * ``location``   -- best-effort string locator
  * ``element``    -- tag name when applicable

The output shape is consumed by:
  * ``_compute_score``      -- surfaces issue counts on the dial.
  * The publication gate    -- counts ``error``-severity findings.
  * The reviewer checklist  -- pre-populates items to confirm.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WcagFinding:
    rule_id: str
    severity: str  # "error" | "warning" | "info"
    wcag_ref: str
    message: str
    location: str = ""
    element: str = ""


@dataclass
class WcagReport:
    findings: list[WcagFinding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    passed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "findings": [asdict(f) for f in self.findings],
            "summary": dict(self.summary),
            "passed": self.passed,
        }


def _location_for(el) -> str:
    """Best-effort locator for an lxml element. Short, human-readable."""
    try:
        path = []
        cur = el
        while cur is not None and getattr(cur, "tag", None):
            tag = cur.tag if isinstance(cur.tag, str) else "node"
            idx_in_parent = ""
            parent = cur.getparent() if hasattr(cur, "getparent") else None
            if parent is not None:
                same = [c for c in parent if getattr(c, "tag", None) == cur.tag]
                if len(same) > 1:
                    idx_in_parent = f"[{same.index(cur) + 1}]"
            path.append(f"{tag}{idx_in_parent}")
            cur = parent
            if len(path) > 6:
                break
        return " > ".join(reversed(path))
    except Exception:
        return ""


def run_wcag_checks(html: str) -> WcagReport:
    """Run the WCAG check set against an HTML document.

    Returns a ``WcagReport``. ``passed`` is ``True`` iff there are zero
    ``error``-severity findings. Warnings don't block publish but
    surface in the reviewer's pre-publish checklist.
    """
    report = WcagReport()
    if not html or not html.strip():
        report.findings.append(WcagFinding(
            rule_id="empty-document",
            severity="error",
            wcag_ref="1.3.1",
            message="HTML document is empty",
        ))
        _finalize(report)
        return report

    try:
        from lxml import html as lxml_html
    except ImportError:
        logger.exception("lxml not installed; WCAG checks cannot run")
        report.findings.append(WcagFinding(
            rule_id="check-runner-unavailable",
            severity="error",
            wcag_ref="-",
            message="WCAG check runner could not be initialized (lxml missing)",
        ))
        _finalize(report)
        return report

    try:
        root = lxml_html.fromstring(html)
    except Exception as exc:
        report.findings.append(WcagFinding(
            rule_id="malformed-html",
            severity="error",
            wcag_ref="4.1.1",
            message=f"HTML failed to parse: {exc!s}",
        ))
        _finalize(report)
        return report

    # Find the <html> element to inspect attributes like ``lang``.
    html_el = root if root.tag == "html" else root.getroottree().getroot() if root.getroottree() else root

    # ----- 1.3.1 / 4.1.2: language and title --------------------------------
    lang = (html_el.get("lang") or html_el.get("xml:lang") or "").strip()
    if not lang:
        report.findings.append(WcagFinding(
            rule_id="lang-attribute-missing",
            severity="error",
            wcag_ref="3.1.1",
            message="<html> is missing a ``lang`` attribute",
            element="html",
        ))
    titles = root.xpath("//title")
    title_text = (titles[0].text_content().strip() if titles else "")
    if not title_text:
        report.findings.append(WcagFinding(
            rule_id="title-missing",
            severity="error",
            wcag_ref="2.4.2",
            message="<title> is missing or empty",
            element="title",
        ))
    elif len(title_text) < 3:
        report.findings.append(WcagFinding(
            rule_id="title-too-short",
            severity="warning",
            wcag_ref="2.4.2",
            message=f"<title> is suspiciously short ({title_text!r}); consider a more descriptive title",
            element="title",
        ))

    # ----- 1.3.1: heading structure ----------------------------------------
    headings = root.xpath("//h1|//h2|//h3|//h4|//h5|//h6")
    h1s = [h for h in headings if h.tag == "h1"]
    if not headings:
        report.findings.append(WcagFinding(
            rule_id="no-headings",
            severity="error",
            wcag_ref="1.3.1",
            message="Document has no headings; this is rarely correct for academic content",
        ))
    elif not h1s:
        report.findings.append(WcagFinding(
            rule_id="no-h1",
            severity="warning",
            wcag_ref="2.4.6",
            message="Document has no <h1>; the page title isn't being represented as a heading",
        ))
    elif len(h1s) > 1:
        report.findings.append(WcagFinding(
            rule_id="multiple-h1",
            severity="warning",
            wcag_ref="2.4.6",
            message=f"Document has {len(h1s)} <h1> elements; usually only one is expected",
        ))

    # Heading-level skip detection: walk in document order.
    prev_level = 0
    for h in headings:
        level = int(h.tag[1])
        if prev_level and level > prev_level + 1:
            report.findings.append(WcagFinding(
                rule_id="heading-level-skip",
                severity="warning",
                wcag_ref="1.3.1",
                message=f"Heading jumps from h{prev_level} to h{level}; intermediate level skipped",
                element=h.tag,
                location=_location_for(h),
            ))
        prev_level = level

    # ----- 1.1.1: images need alt text -------------------------------------
    images = root.xpath("//img")
    for img in images:
        alt = img.get("alt")
        role = (img.get("role") or "").lower()
        aria_hidden = (img.get("aria-hidden") or "").lower() == "true"
        # Decorative images: alt="" OR role=presentation/none OR aria-hidden=true
        decorative = (alt == "" or role in {"presentation", "none"} or aria_hidden)
        if alt is None:
            report.findings.append(WcagFinding(
                rule_id="img-alt-missing",
                severity="error",
                wcag_ref="1.1.1",
                message="<img> is missing an ``alt`` attribute",
                element="img",
                location=_location_for(img),
            ))
        elif decorative:
            # Fine -- but worth flagging for reviewer confirmation.
            report.findings.append(WcagFinding(
                rule_id="img-decorative-needs-confirm",
                severity="info",
                wcag_ref="1.1.1",
                message="Image marked as decorative (empty alt or aria-hidden); confirm this is not content",
                element="img",
                location=_location_for(img),
            ))
        elif len((alt or "").strip()) < 3:
            report.findings.append(WcagFinding(
                rule_id="img-alt-too-short",
                severity="warning",
                wcag_ref="1.1.1",
                message=f"<img> alt text is suspiciously short ({alt!r})",
                element="img",
                location=_location_for(img),
            ))

    # ----- 1.3.1: tables ----------------------------------------------------
    tables = root.xpath("//table")
    for tbl in tables:
        ths = tbl.xpath(".//th")
        if not ths:
            report.findings.append(WcagFinding(
                rule_id="table-missing-th",
                severity="error",
                wcag_ref="1.3.1",
                message="<table> has no <th> elements; data tables need header cells",
                element="table",
                location=_location_for(tbl),
            ))
        else:
            # Check that headers have scope OR the table has headers/id mappings.
            scoped = [th for th in ths if th.get("scope")]
            has_id_headers = bool(tbl.xpath(".//*[@headers]"))
            if not scoped and not has_id_headers:
                report.findings.append(WcagFinding(
                    rule_id="table-th-needs-scope",
                    severity="warning",
                    wcag_ref="1.3.1",
                    message="<th> cells lack ``scope`` (row/col) and no headers/id mapping is present",
                    element="th",
                    location=_location_for(tbl),
                ))
        if not tbl.xpath("./caption"):
            report.findings.append(WcagFinding(
                rule_id="table-no-caption",
                severity="info",
                wcag_ref="1.3.1",
                message="<table> has no <caption>; consider adding one for non-visual users",
                element="table",
                location=_location_for(tbl),
            ))

    # ----- 2.4.4: link names ----------------------------------------------
    for a in root.xpath("//a"):
        text = (a.text_content() or "").strip()
        aria_label = (a.get("aria-label") or "").strip()
        aria_lbl_by = (a.get("aria-labelledby") or "").strip()
        if not text and not aria_label and not aria_lbl_by:
            report.findings.append(WcagFinding(
                rule_id="link-name-missing",
                severity="error",
                wcag_ref="2.4.4",
                message="<a> has no accessible name (no text, aria-label, or aria-labelledby)",
                element="a",
                location=_location_for(a),
            ))
        elif text.lower() in {"click here", "here", "read more", "more"}:
            report.findings.append(WcagFinding(
                rule_id="link-generic-text",
                severity="warning",
                wcag_ref="2.4.4",
                message=f"Link text is generic ({text!r}); use descriptive link text",
                element="a",
                location=_location_for(a),
            ))

    # ----- 2.5.3 / 4.1.2: form/control names (defensive; rare in our output)
    for el in root.xpath("//button|//input"):
        if el.tag == "input" and (el.get("type") or "").lower() in {"hidden", "submit", "button", "reset"}:
            continue
        name = (el.text_content() or "").strip()
        aria_label = (el.get("aria-label") or "").strip()
        if not name and not aria_label:
            report.findings.append(WcagFinding(
                rule_id="control-name-missing",
                severity="error",
                wcag_ref="4.1.2",
                message=f"<{el.tag}> has no accessible name",
                element=el.tag,
                location=_location_for(el),
            ))

    # ----- 1.4.1: color-only indicators (heuristic) -----------------------
    if root.xpath("//font"):
        report.findings.append(WcagFinding(
            rule_id="font-element-present",
            severity="warning",
            wcag_ref="1.4.1",
            message="<font> element is deprecated; styling via CSS only and not color-only indicators",
        ))

    _finalize(report)
    return report


def _finalize(report: WcagReport) -> None:
    """Compute summary counts + overall pass/fail."""
    counts = {"error": 0, "warning": 0, "info": 0}
    for f in report.findings:
        if f.severity in counts:
            counts[f.severity] += 1
    report.summary = counts
    report.passed = counts["error"] == 0
