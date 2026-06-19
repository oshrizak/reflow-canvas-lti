"""Unit tests for the per-user erasure helper.

The full erase walk needs a Redis fixture (covered in the integration
tier when we extend it). The pseudonymisation logic is pure and worth
pinning here — the contract is that the pseudonym is deterministic
given the same user id + pepper, but indistinguishable across users.
"""

from __future__ import annotations

import pytest
from connector.tools.erase_user import _pseudonymize


@pytest.mark.unit
def test_pseudonym_is_deterministic_given_same_pepper(monkeypatch) -> None:
    """Two calls with the same env produce the same pseudonym so audit
    rows pseudonymised at different times still correlate."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-pepper")
    a = _pseudonymize("user-1")
    b = _pseudonymize("user-1")
    assert a == b


@pytest.mark.unit
def test_pseudonym_changes_when_pepper_rotates(monkeypatch) -> None:
    """Rotating the pepper breaks the link between old audit rows and
    the natural user id — feature for handling regulator-driven
    erasure requests."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "pepper-a")
    a = _pseudonymize("user-1")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "pepper-b")
    b = _pseudonymize("user-1")
    assert a != b


@pytest.mark.unit
def test_pseudonym_distinguishes_users(monkeypatch) -> None:
    """Two different users must hash to different pseudonyms even
    under the same pepper. Otherwise erasure conflates them."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-pepper")
    a = _pseudonymize("user-1")
    b = _pseudonymize("user-2")
    assert a != b


@pytest.mark.unit
def test_pseudonym_prefix_marks_erasure(monkeypatch) -> None:
    """The ``erased:`` prefix lets log search distinguish original
    identifiers from pseudonymised ones at a glance."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-pepper")
    assert _pseudonymize("user-1").startswith("erased:")


@pytest.mark.unit
def test_pseudonym_without_pepper_still_works(monkeypatch) -> None:
    """Falls back to CSRF_SECRET_KEY; without that, an empty pepper —
    still produces a deterministic pseudonym, just one that doesn't
    protect against rainbow-table attacks. The dry-run audit row will
    flag this via the CRITICAL the privacy layer logs on the first
    encryption call."""
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("CSRF_SECRET_KEY", raising=False)
    out = _pseudonymize("user-1")
    assert out.startswith("erased:")
    assert len(out) > len("erased:")
