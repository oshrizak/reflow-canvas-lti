#!/usr/bin/env bash
# Generate the RSA keypair the connector uses to sign LTI 1.3 JWTs.
# Public key is published at /lti/jwks.json; private key signs outgoing
# id_tokens and DeepLinking responses.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KEYS_DIR="${KEYS_DIR:-$SCRIPT_DIR/../keys}"
mkdir -p "$KEYS_DIR"

if [[ -f "$KEYS_DIR/lti_private.pem" ]]; then
    echo "Keypair already exists at $KEYS_DIR — refusing to overwrite." >&2
    echo "Delete the files first if you really want to rotate." >&2
    exit 1
fi

openssl genrsa -out "$KEYS_DIR/lti_private.pem" 2048
openssl rsa -in "$KEYS_DIR/lti_private.pem" -pubout -out "$KEYS_DIR/lti_public.pem"
chmod 600 "$KEYS_DIR/lti_private.pem"

echo "Generated:"
echo "  $KEYS_DIR/lti_private.pem"
echo "  $KEYS_DIR/lti_public.pem"
echo
echo "Set in .env:"
echo "  LTI_PRIVATE_KEY_PATH=/app/keys/lti_private.pem"
echo "  LTI_PUBLIC_KEY_PATH=/app/keys/lti_public.pem"
