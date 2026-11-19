"""The cage every Gemini run goes into.

Findings from scripts/sandbox_probe.py (agy 1.5.12, macOS):
- The agy terminal sandbox blocks direct sockets, ~/.ssh, the Gemini config
  and writes outside the working directory.
- It does let HTTP(S) out through its built-in proxy, even with read_url(*)
  on the deny list. The CLI has no network on/off setting.
- It does have `sandboxProxy`, which routes all sandboxed HTTP(S) traffic
  through a proxy of our choice.

So we point `sandboxProxy` at a "black hole": a local port where nothing
listens. agy requires a CA certificate for the proxy, so we generate a
throwaway self-signed one. Its private key is never stored.

Read-only roles additionally get command(*) denied, so they cannot run shell
commands at all.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from . import store

SANDBOX_DIR = store.HOME / "sandbox"
CA_FILE = SANDBOX_DIR / "blackhole-ca.pem"
CA_LOCK = SANDBOX_DIR / "ca.lock"
# Port 9 is the "discard" port; nothing listens there on a normal Mac,
# so connections are refused immediately instead of hanging.
BLACKHOLE_ADDRESS = "127.0.0.1:9"


def ensure_dummy_ca() -> Path:
    """Create a throwaway self-signed CA certificate if it does not exist yet."""
    if CA_FILE.exists():
        return CA_FILE
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    store.ensure_dir(SANDBOX_DIR)
    with store.locked(CA_LOCK):
        if CA_FILE.exists():
            return CA_FILE
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ai-bridge blackhole (no network)")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        store.atomic_write(CA_FILE, cert.public_bytes(serialization.Encoding.PEM).decode())
    return CA_FILE


def no_network_settings() -> dict:
    """agy settings fragment that sends all sandboxed traffic into the black hole."""
    return {
        "sandboxProxy": {
            "http": {
                "address": BLACKHOLE_ADDRESS,
                "certFile": str(ensure_dummy_ca()),
            }
        }
    }


def agy_settings(deny_commands: list[str], *, read_only: bool, web: bool) -> dict:
    """Full per-run settings for agy (they are added on top of the global settings)."""
    deny = [f"command({c})" for c in deny_commands]
    if read_only:
        deny.append("command(*)")
    return {
        "enableTerminalSandbox": True,
        "toolPermission": "proceed-in-sandbox",
        **no_network_settings(),
        "permissions": {
            "allow": ["read_url(*)"] if web else [],
            "deny": deny,
        },
    }


def write_settings(path: Path, settings: dict) -> Path:
    store.ensure_dir(path.parent)
    store.atomic_write(path, json.dumps(settings, indent=2))
    return path
