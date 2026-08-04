"""Synthesize per-node launch artifacts from a topology.

For relays, emit a moqx YAML mounted into the container at /etc/moqx/relay.yaml.
For publishers and subscribers, emit the CLI flag list passed to moqdateserver /
moqtextclient (their entrypoints already point at the binaries).

All URLs use container DNS — the Docker backend creates every node with
`name=<id>` on a single user-defined bridge network, and the Containernet
backend assigns `hostname=<id>` on the Mininet net, so `https://<id>:<port>/…`
just works on both.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from moqlab.certs import TLS_CERT, TLS_KEY
from moqlab.config.schema import (
    CacheConfig,
    TlsConfig,
    TopologyConfig,
)


def _tls_to_yaml(tls: TlsConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"insecure": tls.insecure}
    if tls.generated:
        out["cert_file"] = TLS_CERT
        out["key_file"] = TLS_KEY
    elif tls.cert_file:
        out["cert_file"] = tls.cert_file
    if tls.key_file:
        out["key_file"] = tls.key_file
    if tls.ca_cert:
        out["ca_cert"] = tls.ca_cert
    return out


def _cache_to_yaml(cache: CacheConfig) -> dict[str, Any]:
    return {
        "enabled": cache.enabled,
        "max_tracks": cache.max_tracks,
        "max_groups_per_track": cache.max_groups_per_track,
    }


# ── relays ─────────────────────────────────────────────────────────────────


def synthesize_relay_yaml(topology: TopologyConfig, relay_id: str) -> dict[str, Any]:
    """Return the moqx YAML for one relay as a Python dict."""
    if relay_id not in topology.relays:
        raise KeyError(f"unknown relay id: {relay_id}")

    r = topology.relays[relay_id]
    endpoint = topology.relay_endpoint(relay_id)
    tls = topology.relay_tls(relay_id)
    cache = topology.relay_cache(relay_id)

    service: dict[str, Any] = {
        "match": [{"authority": {"any": True}, "path": {"exact": endpoint}}],
        "cache": _cache_to_yaml(cache),
    }

    media_origin = next(
        (
            (pid, publisher)
            for pid, publisher in topology.publishers.items()
            if publisher.kind == "media" and publisher.connects_to == relay_id
        ),
        None,
    )
    if media_origin is not None:
        pid, publisher = media_origin
        service["upstream"] = {
            "url": f"moqt://{pid}:{publisher.listen_port}/moq",
            "tls": {"insecure": True},
        }
    elif r.upstream is not None:
        u_endpoint = topology.relay_endpoint(r.upstream)
        u_tls = topology.relay_tls(r.upstream)
        u_port = topology.relays[r.upstream].listen_port
        upstream_tls = _tls_to_yaml(u_tls)
        if u_tls.generated:
            # moqx currently ignores upstream.tls.ca_cert. Keep the hop
            # encrypted, but disable upstream verification for generated
            # self-signed run certificates.
            upstream_tls = {"insecure": True}
        service["upstream"] = {
            "url": f"moqt://{r.upstream}:{u_port}{u_endpoint}",
            "tls": upstream_tls,
        }

    return {
        "relay_id": relay_id,
        "listeners": [
            {
                "name": "main",
                "udp": {"socket": {"address": "::", "port": r.listen_port}},
                "tls": _tls_to_yaml(tls),
                "endpoint": endpoint,
            }
        ],
        "services": {"default": service},
        "admin": {
            "port": r.admin_port,
            "address": "::",
            "plaintext": True,
        },
    }


def synthesize_relay_configs(
    topology: TopologyConfig, out_dir: str | Path
) -> dict[str, Path]:
    """Write one moqx YAML per relay under `out_dir/`. Returns relay_id -> path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for rid in topology.relays:
        doc = synthesize_relay_yaml(topology, rid)
        path = out / f"{rid}.yaml"
        with path.open("w") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
        written[rid] = path
    return written


# ── publishers & subscribers ───────────────────────────────────────────────


def _relay_client_url(topology: TopologyConfig, relay_id: str) -> str:
    """The https:// URL clients (pubs/subs) use to talk to a relay."""
    r = topology.relays[relay_id]
    endpoint = topology.relay_endpoint(relay_id)
    return f"https://{relay_id}:{r.listen_port}{endpoint}"


def synthesize_publisher_command(
    topology: TopologyConfig, publisher_id: str
) -> list[str]:
    """Return the moqdateserver argv (without the binary path itself).

    The image's ENTRYPOINT is /usr/local/bin/moqdateserver, so Docker prepends
    it automatically. We return only the flag list.
    """
    if publisher_id not in topology.publishers:
        raise KeyError(f"unknown publisher id: {publisher_id}")
    p = topology.publishers[publisher_id]

    if p.kind == "media":
        return [
            "-addr", f"0.0.0.0:{p.listen_port}",
            "-asset", f"/opt/moqlivemock/assets/{p.asset}",
            "-cert", TLS_CERT,
            "-key", TLS_KEY,
            "-sideport", str(p.fingerprint_port),
            "-qlog", f"/tmp/{publisher_id}.qlog",
        ]

    argv = [
        f"--ns={p.namespace}",
        f"--relay_url={_relay_client_url(topology, p.connects_to)}",
        f"--logging={topology.publisher_log_level(publisher_id)}",
    ]
    if topology.publisher_insecure(publisher_id):
        argv.append("--insecure")
    if p.port is not None:
        argv.append(f"--port={p.port}")
    return argv


def synthesize_subscriber_command(
    topology: TopologyConfig, subscriber_id: str
) -> list[str]:
    """Return the moqtextclient argv (without the binary path itself)."""
    if subscriber_id not in topology.subscribers:
        raise KeyError(f"unknown subscriber id: {subscriber_id}")
    s = topology.subscribers[subscriber_id]

    if s.kind == "media":
        media_publisher = topology.media_publisher_for_relay(s.connects_to)
        return [
            f"--server-url={_relay_client_url(topology, s.connects_to)}",
            "--fingerprint-url="
            f"http://{media_publisher[0]}:"
            f"{media_publisher[1].fingerprint_port}/fingerprint",
            f"--namespace={s.namespace}",
            f"--video-track={s.track}",
            f"--browser-mode={s.browser_mode}",
            f"--minimal-buffer-ms={s.minimal_buffer_ms}",
            f"--target-latency-ms={s.target_latency_ms}",
            f"--ready-timeout-s={topology.startup.media_ready_timeout_s:g}",
        ]

    argv = [
        f"--connect_url={_relay_client_url(topology, s.connects_to)}",
        f"--track_namespace={s.namespace}",
        f"--track_name={s.track}",
        f"--logging={topology.subscriber_log_level(subscriber_id)}",
    ]
    if topology.subscriber_insecure(subscriber_id):
        argv.append("--insecure")
    return argv
