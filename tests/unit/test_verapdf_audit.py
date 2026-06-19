"""Unit tests for the VeraPDF report parser.

The audit subprocess isn't unit-tested here — that needs the verapdf
binary installed which is only present inside the connector image. The
JSON parsing path is pure, so we cover it with the report shapes
VeraPDF actually emits.
"""

import pytest
from connector.canvas.verapdf_audit import _parse_report


@pytest.mark.unit
def test_parse_clean_report() -> None:
    """Compliant PDF: every applicable rule passed."""
    payload = {
        "report": {
            "jobs": [
                {
                    "validationResult": {
                        "compliant": True,
                        "details": {
                            "passedRules": 30,
                            "failedRules": 0,
                            "failedChecks": 0,
                            "ruleSummaries": [],
                        },
                    }
                }
            ]
        }
    }
    r = _parse_report(payload, flavour="ua1")

    assert r.score == 100
    assert r.passed_rules == 30
    assert r.failed_rules == 0
    assert r.total_occurrences == 0
    assert r.violations == []
    assert r.is_compliant is True


@pytest.mark.unit
def test_parse_failing_report() -> None:
    """Non-compliant PDF: score reflects pass/fail ratio + violations populated."""
    payload = {
        "report": {
            "jobs": [
                {
                    "validationResult": {
                        "compliant": False,
                        "details": {
                            "passedRules": 24,
                            "failedRules": 6,
                            "failedChecks": 15,
                            "ruleSummaries": [
                                {
                                    "ruleStatus": "FAILED",
                                    "clause": "7.1",
                                    "testNumber": 1,
                                    "description": "All real content must be marked.",
                                    "failedChecks": 4,
                                },
                                {
                                    "ruleStatus": "FAILED",
                                    "clause": "7.18.3",
                                    "testNumber": 2,
                                    "description": "Image of text must have alt text.",
                                    "failedChecks": 11,
                                },
                                {
                                    # A rule that's reported but passed —
                                    # should NOT be counted as a violation.
                                    "ruleStatus": "PASSED",
                                    "clause": "7.2",
                                    "testNumber": 1,
                                    "description": "Document language declared.",
                                    "failedChecks": 0,
                                },
                            ],
                        },
                    }
                }
            ]
        }
    }
    r = _parse_report(payload, flavour="ua1")

    # 24 / 30 = 0.8 -> 80%
    assert r.score == 80
    assert r.passed_rules == 24
    assert r.failed_rules == 6
    assert r.total_occurrences == 15
    assert r.is_compliant is False
    assert len(r.violations) == 2
    assert {v.rule_id for v in r.violations} == {"7.1-1", "7.18.3-2"}


@pytest.mark.unit
def test_parse_empty_report() -> None:
    """No jobs at all — graceful zero, not an exception."""
    payload = {"report": {"jobs": []}}
    r = _parse_report(payload, flavour="ua1")

    assert r.score == 0
    assert r.passed_rules == 0
    assert r.failed_rules == 0
    assert r.is_compliant is False
    assert r.violations == []
