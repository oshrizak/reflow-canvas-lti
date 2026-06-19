"""Privacy + retention controls for canvas/Reflow jobs.

Three Title-II / institutional-compliance concerns this module owns:

  1. **Deletion.** A documented way for an admin to remove a job and
     every derivative artifact -- source PDF, markdown, figures,
     edited HTML, score cache, audit references. Required for FERPA-
     style "right to deletion" / DSAR responses and for the routine
     "remove this document from the system" workflow.

  2. **Retention.** Configurable max-age for each artifact class.
     A periodic sweep enforces them. Defaults are conservative for
     a higher-ed deployment (90 days hot, retain audit packets
     longer per institutional policy).

  3. **Token encryption.** OAuth user tokens are sensitive long-lived
     bearers. We encrypt them at rest with a key derived from
     ``CSRF_SECRET_KEY`` (or a dedicated ``TOKEN_ENCRYPTION_KEY`` if
     set) before storing in Redis. The decrypt happens in the
     ``user_oauth`` code path on token read; both sides go through
     ``encrypt_secret`` / ``decrypt_secret`` here.

Anti-claim: ``data at rest`` is a deployment concern. Redis itself
needs auth + TLS + private networking to be genuinely at-rest-secure.
This module raises the bar over plaintext storage but is not a
replacement for properly hardened Redis.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# -- Retention policy --------------------------------------------------------

@dataclass(frozen=True)
class RetentionPolicy:
    """How long each artifact class is kept before the sweeper removes it.

    All values in days. ``None`` means "keep indefinitely" (which is
    appropriate only for audit packets per institutional record-keeping
    policy; everything else should have a finite TTL).
    """

    source_pdf_days: int | None = 30
    markdown_days: int | None = 90
    figures_days: int | None = 90
    edited_html_days: int | None = 365
    audio_days: int | None = 90
    epub_days: int | None = 90
    pii_findings_days: int | None = 90
    audit_packet_days: int | None = None  # None = institutional retention policy
    logs_days: int | None = 90


DEFAULT_RETENTION = RetentionPolicy()


# -- Token encryption --------------------------------------------------------

_constant_fallback_warned = False


def _encryption_key() -> bytes:
    """Derive a stable 32-byte key from configured secrets.

    Preferred: explicit ``TOKEN_ENCRYPTION_KEY`` env. Fallback: derive
    from ``CSRF_SECRET_KEY`` via SHA-256. Either way the key never hits
    Redis and rotates with the underlying secret.

    Last resort fallback: a hardcoded constant. ANY attacker with this
    repo can decrypt your Redis dump in that mode — so we log a CRITICAL
    once per process to make it impossible to miss in production logs.
    Production deployments MUST set one of the two env vars.
    """
    explicit = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).digest()
    csrf = os.environ.get("CSRF_SECRET_KEY", "").strip()
    if csrf:
        return hashlib.sha256(("token:" + csrf).encode("utf-8")).digest()
    global _constant_fallback_warned
    if not _constant_fallback_warned:
        logger.critical(
            "TOKEN_ENCRYPTION_KEY and CSRF_SECRET_KEY are BOTH unset. "
            "OAuth tokens (instructor impersonation credentials) are now "
            "being encrypted with a HARDCODED key any reader of this "
            "source can derive. This is UNSAFE for production. Generate "
            "a key with: python -m connector.tools.generate_keys "
            "and paste the output into .env."
        )
        _constant_fallback_warned = True
    return hashlib.sha256(b"equalify-reflow:token-encryption:v1").digest()


def encrypt_secret(plain: str) -> str:
    """Encrypt a secret string for at-rest storage.

    Uses an AEAD construction (AES-GCM if ``cryptography`` is
    available, otherwise an HMAC-SHA256-authenticated XOR stream).
    The output is base64-url-encoded and self-describing: the first
    byte identifies the cipher so future rotations can co-exist with
    legacy ciphertexts during a transition window.
    """
    if not plain:
        return ""
    key = _encryption_key()
    data = plain.encode("utf-8")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, data, associated_data=b"reflow:tok:v1")
        # Format: [0x01][nonce 12B][ciphertext]
        blob = b"\x01" + nonce + ct
    except Exception:
        # Fallback path. NOT semantically equivalent to AEAD -- we
        # XOR with a keystream derived from HMAC-SHA256 over a
        # counter, then HMAC-tag the ciphertext. Use only when
        # cryptography isn't available, which shouldn't happen in
        # the current container.
        logger.warning("AESGCM unavailable; falling back to HMAC-XOR cipher")
        nonce = os.urandom(16)
        stream = b""
        ctr = 0
        while len(stream) < len(data):
            stream += hmac.new(key, nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
            ctr += 1
        ct = bytes(a ^ b for a, b in zip(data, stream))
        tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        blob = b"\x02" + nonce + tag + ct
    return base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_secret(encoded: str) -> str:
    """Inverse of ``encrypt_secret``. Returns the plaintext.

    Idempotent on legacy plaintext: if the input doesn't look like
    one of our ciphertexts (no ``\\x01`` / ``\\x02`` prefix after
    base64-decoding), we assume it was stored before encryption
    landed and return it as-is. This lets us roll out encryption
    without a breaking migration.
    """
    if not encoded:
        return ""
    try:
        blob = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception:
        return encoded  # legacy plaintext, can't decode
    if not blob:
        return ""
    key = _encryption_key()
    version = blob[0]
    if version == 0x01:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise RuntimeError("Encrypted token requires cryptography lib")
        nonce, ct = blob[1:13], blob[13:]
        try:
            return AESGCM(key).decrypt(nonce, ct, b"reflow:tok:v1").decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Token decrypt failed: {exc}") from exc
    if version == 0x02:
        nonce, tag, ct = blob[1:17], blob[17:49], blob[49:]
        expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise RuntimeError("Token tag mismatch (possible tampering)")
        stream = b""
        ctr = 0
        while len(stream) < len(ct):
            stream += hmac.new(key, nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
            ctr += 1
        return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")
    # Unrecognised prefix -- assume legacy plaintext that happened to
    # base64-decode cleanly. Better to return it than 500.
    return encoded


# -- Deletion ----------------------------------------------------------------

async def delete_job_and_derivatives(redis: Any, *, reflow_job_id: str) -> dict[str, Any]:
    """Hard-delete a job and every related artifact in Redis.

    Returns a summary of what was removed. Does not touch S3 (that's
    a separate worker job -- the S3 keys are in the deleted records
    so a sweeper can reconcile). Audit log entries about the deletion
    are KEPT in ``eq-pdf:canvas:approval:audit`` (immutable trail).
    """
    from ..canvas.tenant import tk

    if not reflow_job_id:
        raise ValueError("reflow_job_id is required")

    deleted = {
        "canvas_bridge": 0, "reflow_record": 0, "edited_html": 0,
        "score_cache": 0, "approval_token": 0,
    }
    keys_to_remove = [
        tk(f"canvas:job:{reflow_job_id}"),
        f"eq-pdf:job:{reflow_job_id}",
        tk(f"canvas:edited:{reflow_job_id}"),
        tk(f"canvas:score:{reflow_job_id}"),
    ]
    for k in keys_to_remove:
        try:
            n = await redis.delete(k)
        except Exception:
            logger.exception("delete_job: redis.delete failed on %s", k)
            continue
        if n:
            tag = "canvas_bridge" if "canvas:job" in k else (
                "reflow_record" if k.endswith(reflow_job_id) and "canvas" not in k else
                "edited_html" if "canvas:edited" in k else "score_cache"
            )
            deleted[tag] = int(n)

    # Best-effort approval-token reverse index cleanup.
    try:
        cursor = 0
        while True:
            cursor, ks = await redis.scan(cursor=cursor, match="eq-pdf:approval-token:*", count=200)
            for raw in ks:
                tk_key = raw.decode() if isinstance(raw, bytes) else raw
                jid = await redis.get(tk_key)
                if jid:
                    val = jid.decode() if isinstance(jid, bytes) else jid
                    if val == reflow_job_id:
                        await redis.delete(tk_key)
                        deleted["approval_token"] += 1
            if cursor == 0:
                break
    except Exception:
        logger.exception("delete_job: approval-token sweep failed")

    logger.info(
        "delete_job %s removed: %s",
        reflow_job_id,
        ", ".join(f"{k}={v}" for k, v in deleted.items() if v),
    )
    return deleted
