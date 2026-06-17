"""RSA keypair management for LTI 1.3.

Canvas validates our outgoing assertions against the JWKS we publish at
``/lti/jwks``. The private key signs assertions; the public key, in JWK
form, is what Canvas fetches.

This module is intentionally minimal and *file-backed* — keys live on a
mounted volume in dev (``./keys``) and a secret-mount in prod. We do not
load keys from env vars to avoid logging them by accident.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwcrypto import jwk

from .config import get_lti_settings

logger = logging.getLogger(__name__)

RSA_KEY_SIZE = 2048
RSA_PUBLIC_EXPONENT = 65537


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def generate_keypair(private_path: Path, public_path: Path) -> None:
    """Generate a fresh RSA keypair and write PEM files.

    Refuses to overwrite an existing private key — rotation is a separate
    operation that requires explicit removal of the old file.
    """

    if private_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing private key at {private_path}. "
            "Delete the file explicitly to rotate."
        )

    _ensure_parent(private_path)
    _ensure_parent(public_path)

    private_key: RSAPrivateKey = rsa.generate_private_key(
        public_exponent=RSA_PUBLIC_EXPONENT,
        key_size=RSA_KEY_SIZE,
    )

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    private_path.chmod(0o600)
    public_path.chmod(0o644)

    logger.info("Wrote LTI keypair: %s, %s", private_path, public_path)


def load_private_key() -> RSAPrivateKey:
    cfg = get_lti_settings()
    path = Path(cfg.private_key_path)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(f"Expected RSA private key at {path}, got {type(key).__name__}")
    return key


def load_public_key() -> RSAPublicKey:
    cfg = get_lti_settings()
    path = Path(cfg.public_key_path)
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, RSAPublicKey):
        raise TypeError(f"Expected RSA public key at {path}, got {type(key).__name__}")
    return key


def jwks_document() -> dict[str, Any]:
    """Return our JWKS as a plain dict, suitable for FastAPI to JSON-encode.

    Canvas pulls this from ``/lti/jwks`` periodically and caches it; the
    kid is stable across restarts because it is derived from the key.
    """

    cfg = get_lti_settings()
    pem = Path(cfg.public_key_path).read_bytes()
    key = jwk.JWK.from_pem(pem)
    public_jwk = key.export_public(as_dict=True)
    # jwcrypto sets kid from a thumbprint when from_pem is used; if not, fall back.
    public_jwk.setdefault("kid", key.thumbprint())
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return {"keys": [public_jwk]}


def _cli() -> None:
    """Tiny CLI: ``python -m connector.lti.keys generate``."""

    parser = argparse.ArgumentParser(description="LTI keypair utilities")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="Generate a new RSA keypair if none exists")
    args = parser.parse_args()

    cfg = get_lti_settings()
    if args.cmd == "generate":
        generate_keypair(Path(cfg.private_key_path), Path(cfg.public_key_path))


if __name__ == "__main__":
    _cli()
