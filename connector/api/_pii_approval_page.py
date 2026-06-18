"""Self-contained HTML page rendered when a job hits the PII gate.

Faculty members hitting an ``alt/{job_id}/html`` URL on a job whose
Reflow status is ``awaiting_approval`` get this page instead of the
raw JSON 409. It summarises the PII Reflow detected, lets the faculty
member type a justification, and POSTs an approve/deny decision to
``/canvas/panorama/pii-decision/{job_id}``.

The page is self-contained: a tiny CSS stylesheet inline, a single
JS function that submits the form via ``fetch`` and reloads on
success. No external assets — Canvas's iframe sandbox blocks third-
party origins by default, so any CDN reference would be a hassle.

Why the gate exists: Reflow's PII scan flags entities like person
names, emails, phone numbers, addresses, dates of birth, ID numbers,
etc. For most academic documents the false-positive rate is high
(student names in syllabus authorship, dates of birth in historical
material, addresses in citations). The faculty member is the right
human to decide whether the detected items are sensitive given the
document's intended audience.
"""

from __future__ import annotations

import html as _html
from collections import Counter
from typing import Any


def _safe(text: str | None) -> str:
    return _html.escape(str(text or ""))


def _summarize_findings(findings: list[dict[str, Any]]) -> list[tuple[str, int, float]]:
    """Group PII findings into ``(entity_type, count, max_score)`` rows.

    We don't echo the actual flagged spans on the page — those are the
    sensitive bits we're asking permission to process. The faculty
    member already has the source PDF open elsewhere; showing them a
    typed summary ("3 EMAIL, 1 PHONE") is enough to make the call
    without re-exposing the data in our UI.
    """
    if not findings:
        return []
    counter: Counter[str] = Counter()
    max_score: dict[str, float] = {}
    for f in findings:
        etype = (
            f.get("entity_type")
            or f.get("type")
            or f.get("label")
            or "UNKNOWN"
        )
        counter[etype] += 1
        try:
            s = float(f.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            s = 0.0
        if s > max_score.get(etype, 0.0):
            max_score[etype] = s
    rows = [(etype, count, max_score.get(etype, 0.0)) for etype, count in counter.items()]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def render_pii_approval_page(
    *,
    job_id: str,
    file_name: str | None,
    course_id: str | None,
    findings: list[dict[str, Any]],
    decision_url: str,
) -> str:
    """Return a complete HTML document with the PII approval form."""

    rows = _summarize_findings(findings)
    if rows:
        body_rows = "\n".join(
            f"        <tr><td>{_safe(etype)}</td><td>{count}</td>"
            f"<td>{score:.2f}</td></tr>"
            for etype, count, score in rows
        )
        findings_table = f"""
    <table aria-describedby="findings-caption">
      <caption id="findings-caption">Detected categories of PII in this document</caption>
      <thead><tr><th scope="col">Category</th><th scope="col">Count</th><th scope="col">Top confidence</th></tr></thead>
      <tbody>
{body_rows}
      </tbody>
    </table>"""
    else:
        # Defensive: Reflow occasionally pauses with no findings list
        # populated (race between status update and finding write).
        findings_table = (
            "<p><em>Reflow paused this document for PII review but no detailed "
            "findings are available. You can still approve to proceed.</em></p>"
        )

    title = _safe(file_name) or "this document"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PII review · {title}</title>
  <style>
    :root {{
      --bg: #f7f8fa;
      --card: #fff;
      --ink: #1a1f29;
      --muted: #5b6573;
      --line: #e3e6eb;
      --accent: #2563eb;
      --approve: #15803d;
      --approve-hover: #166534;
      --deny: #b91c1c;
      --deny-hover: #991b1b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      margin: 0;
      padding: 32px 16px;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 28px 32px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }}
    h1 {{ font-size: 20px; margin: 0 0 8px; }}
    h2 {{ font-size: 16px; margin: 24px 0 8px; }}
    p {{ margin: 8px 0; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0 24px;
      font-size: 14px;
    }}
    caption {{ text-align: left; color: var(--muted); padding-bottom: 8px; font-size: 13px; }}
    th, td {{
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }}
    th {{ background: #f0f3f7; font-weight: 600; }}
    label {{ display: block; font-weight: 600; margin: 16px 0 6px; }}
    textarea {{
      width: 100%;
      min-height: 96px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
      resize: vertical;
    }}
    textarea:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
    .actions {{ display: flex; gap: 12px; margin-top: 20px; }}
    button {{
      flex: 1;
      padding: 10px 16px;
      font: inherit;
      font-weight: 600;
      color: #fff;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
    }}
    button[disabled] {{ opacity: 0.5; cursor: not-allowed; }}
    .approve {{ background: var(--approve); }}
    .approve:hover:not([disabled]) {{ background: var(--approve-hover); }}
    .deny {{ background: var(--deny); }}
    .deny:hover:not([disabled]) {{ background: var(--deny-hover); }}
    #status {{
      margin-top: 16px;
      padding: 10px 14px;
      border-radius: 6px;
      font-size: 14px;
      display: none;
    }}
    #status.error {{ background: #fee2e2; color: #7f1d1d; display: block; }}
    #status.ok {{ background: #dcfce7; color: #14532d; display: block; }}
    .meta {{ font-size: 13px; color: var(--muted); }}
  </style>
</head>
<body>
<main>
  <h1>Privacy review needed</h1>
  <p>Equalify Reflow detected possible personally identifiable
  information (PII) in <strong>{title}</strong> and paused before
  generating the accessible version. As the instructor, please review
  the categories below and decide whether to proceed.</p>

  <p class="muted">If you approve, Reflow continues generating
  alternative formats (HTML, audio, EPUB, etc.). If you deny, the
  source PDF is dropped from our system and no derived files are kept.</p>

  <h2>What Reflow flagged</h2>
{findings_table}

  <form id="pii-form" novalidate>
    <label for="justification">Justification (required, ≥ 10 characters)</label>
    <textarea id="justification" name="justification"
              minlength="10" maxlength="1000"
              placeholder="e.g. The flagged items are author bylines and citations in a published academic article, not student records."></textarea>

    <div class="actions">
      <button type="button" class="approve" id="approve-btn">Approve and process</button>
      <button type="button" class="deny" id="deny-btn">Deny (don't process)</button>
    </div>

    <div id="status" role="status" aria-live="polite"></div>

    <p class="meta">Job ID: <code>{_safe(job_id)}</code><br>
    Course ID: <code>{_safe(course_id)}</code></p>
  </form>
</main>

<script>
(function () {{
  const decisionUrl = {decision_url!r};
  const statusBox = document.getElementById("status");
  const justify = document.getElementById("justification");
  const buttons = [document.getElementById("approve-btn"), document.getElementById("deny-btn")];

  function setMsg(kind, text) {{
    statusBox.className = kind;
    statusBox.textContent = text;
  }}

  async function submit(decision) {{
    const justification = (justify.value || "").trim();
    if (justification.length < 10) {{
      setMsg("error", "Please enter at least 10 characters of justification.");
      justify.focus();
      return;
    }}
    buttons.forEach((b) => (b.disabled = true));
    setMsg("ok", decision === "approved"
      ? "Submitting approval — Reflow will continue processing…"
      : "Submitting denial — the source file will be removed…");
    try {{
      const resp = await fetch(decisionUrl, {{
        method: "POST",
        credentials: "include",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ decision: decision, justification: justification }}),
      }});
      const payload = await resp.json().catch(() => ({{}}));
      if (!resp.ok) {{
        throw new Error(payload.detail || ("HTTP " + resp.status));
      }}
      if (decision === "approved") {{
        setMsg("ok", "Approved. Reload this page in ~30 seconds to view the accessible version.");
      }} else {{
        setMsg("ok", "Denied. This document will not be processed.");
      }}
    }} catch (err) {{
      setMsg("error", "Submission failed: " + (err && err.message ? err.message : err));
      buttons.forEach((b) => (b.disabled = false));
    }}
  }}

  document.getElementById("approve-btn").addEventListener("click", () => submit("approved"));
  document.getElementById("deny-btn").addEventListener("click", () => submit("denied"));
}})();
</script>
</body>
</html>
"""
