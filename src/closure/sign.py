"""Ed25519 signatures on certificates, so an edit that keeps the arithmetic consistent (a new
question, a widened window, a renamed part) is still caught. Verifiers pin the issuer's public key;
a certificate re-signed with another key fails against the pinned one.

The private key lives in data/ (git-ignored). Never commit it.
"""
import json, os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from .paypal import ROOT

KEY = os.path.join(ROOT, "data", "signing-key.pem")


def _body(cert):
    return json.dumps({k: v for k, v in cert.items() if k != "signature"},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def private_key(path=KEY):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 key")
    return key


def public_hex(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def sign(cert, key=None):
    key = key or private_key()
    out = dict(cert)
    out.pop("signature", None)
    out["signature"] = {"alg": "ed25519", "public_key": public_hex(key), "value": key.sign(_body(out)).hex()}
    return out


def verify(cert, trusted_public_hex):
    """True only if the certificate is signed by the pinned key and unchanged since."""
    sig = cert.get("signature") or {}
    if sig.get("alg") != "ed25519" or sig.get("public_key") != trusted_public_hex:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(trusted_public_hex)).verify(bytes.fromhex(sig["value"]), _body(cert))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False
