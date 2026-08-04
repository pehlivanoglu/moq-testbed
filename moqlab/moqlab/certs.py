"""Per-run TLS material shared by media origins and relays."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import OrchestratorError

TLS_MOUNT = "/run/moqlab/tls"
TLS_CERT = f"{TLS_MOUNT}/cert.pem"
TLS_KEY = f"{TLS_MOUNT}/key.pem"


def generate_run_tls(topology: TopologyConfig, run_dir: Path) -> Path | None:
    """Create the shared ECDSA certificate when generated TLS is selected."""
    if not any(topology.relay_tls(rid).generated for rid in topology.relays):
        return None

    tls_dir = run_dir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    key = tls_dir / "key.pem"
    cert = tls_dir / "cert.pem"
    dns_names = sorted({*topology.relays, *topology.publishers, "localhost"})
    sans = ",".join([*(f"DNS:{name}" for name in dns_names), "IP:127.0.0.1"])
    try:
        subprocess.run(
            [
                "openssl",
                "ecparam",
                "-name",
                "prime256v1",
                "-genkey",
                "-noout",
                "-out",
                str(key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-x509",
                "-key",
                str(key),
                "-out",
                str(cert),
                "-days",
                "13",
                "-sha256",
                "-subj",
                "/CN=moqlab",
                "-addext",
                f"subjectAltName={sans}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise OrchestratorError("OpenSSL is required for generated TLS") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise OrchestratorError(f"failed to generate run TLS: {detail}") from e
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    return tls_dir
