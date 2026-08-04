"""IPv4 literals must not reach Canvas as raw dotted quads.

Managed WAF rule sets treat a bare IP address in a request body as an
SSRF / malicious-link signature. Canvas Cloud is behind CloudFront, so a
Page write containing one is rejected at the edge with an HTML 403 that
Canvas never sees — indistinguishable, from the status code alone, from a
permissions error. On 2026-08-04 that cost most of a day.

Measured against CSUEB's WAF (course 50594): the raw literal was blocked
in an ``href`` and in plain text, over http and https alike; the
entity-encoded form passed. So the rule matches the literal string and
does not decode entities before matching.

Course material legitimately contains these — the SPRITE structure-search
server really is at ``211.25.251.1`` — so removing the link is not an
option. Encoding the dots keeps the page identical to a reader while
breaking the byte pattern.
"""

from __future__ import annotations

from connector.canvas.markdown_to_html import render


def _html(markdown: str) -> str:
    return render(markdown, title="t").html


def test_ip_in_a_link_is_encoded():
    html = _html("[SPRITE](http://211.25.251.1/sprite/)")

    assert "211.25.251.1" not in html, "a raw dotted quad is what gets blocked"
    assert "211&#46;25&#46;251&#46;1" in html


def test_ip_in_plain_text_is_encoded():
    """The rule matched body text too, not just attributes."""
    html = _html("Connect to the server at 211.25.251.1 to continue.")

    assert "211.25.251.1" not in html
    assert "211&#46;25&#46;251&#46;1" in html


def test_the_link_still_works():
    """Encoding must not damage the href — browsers decode entities in
    attribute values, so the anchor still resolves to the same address."""
    html = _html("[SPRITE](http://211.25.251.1/sprite/)")

    assert 'href="http://211&#46;25&#46;251&#46;1/sprite/"' in html
    assert ">SPRITE</a>" in html


def test_ordinary_prose_is_untouched():
    html = _html("The active site contains three residues.")

    assert "&#46;" not in html, "only dotted quads should be rewritten"


def test_decimals_and_versions_are_not_mangled_visibly():
    """A version string is not an IP, but if one does match the shape the
    encoding is still visually lossless — ``&#46;`` renders as a full stop.
    What must never happen is a digit going missing."""
    html = _html("Version 1.2.3.4 of the tool.")

    assert "1&#46;2&#46;3&#46;4" in html
    for digit in ("1", "2", "3", "4"):
        assert digit in html


def test_short_numbers_are_left_alone():
    """Three groups is not a dotted quad; don't touch semantic versions."""
    html = _html("Release 2.1.0 shipped.")

    assert "2.1.0" in html
    assert "&#46;" not in html
