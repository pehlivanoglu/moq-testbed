"""Pydantic v2 schema for the moqlab topology config.

A topology is a YAML file with four top-level node-bearing blocks (relays,
publishers, subscribers, optional links) plus inheritable defaults. Every node
id is unique across the whole topology — it becomes a Docker container name
and the DNS label other nodes use to reach it.

Schema invariants enforced here:
  - node ids globally unique across relays/publishers/subscribers
  - upstream / connects_to references resolve to a known relay
  - all relay listen_port + admin_port values unique (host-port pool)
  - single upstream per relay, no cycles in the upstream chain
  - links reference known nodes, each undirected pair appears at most once
  - generative mode rejected (v1 explicit only)

The `links:` block is read by the Containernet backend (Phase 2) for tc/netem
shaping. The Docker backend ignores it; that is by design, so the same config
runs unchanged on either backend.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from moqlab.exceptions import ConfigError

_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_MIN_PORT = 1024
_MAX_PORT = 65535
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class _StrictBase(BaseModel):
    """Reject unknown fields so typos surface as ConfigError, not silent drops."""

    model_config = ConfigDict(extra="forbid", frozen=False)


# ── leaf config blocks ─────────────────────────────────────────────────────


class TlsConfig(_StrictBase):
    insecure: bool = True
    cert_file: str | None = None
    key_file: str | None = None
    ca_cert: str | None = None

    @model_validator(mode="after")
    def _check_cert_pair(self) -> "TlsConfig":
        if not self.insecure and not (self.cert_file and self.key_file):
            raise ValueError("tls.insecure=false requires cert_file and key_file")
        return self


class CacheConfig(_StrictBase):
    enabled: bool = False
    max_tracks: int = Field(default=100, gt=0)
    max_groups_per_track: int = Field(default=3, gt=0)


# ── defaults per node kind ─────────────────────────────────────────────────


class RelayDefaults(_StrictBase):
    image: str = "moqlab-relay"
    endpoint: str = "/moq-relay"
    tls: TlsConfig = Field(default_factory=TlsConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @model_validator(mode="after")
    def _check_endpoint(self) -> "RelayDefaults":
        if not self.endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        return self


class PublisherDefaults(_StrictBase):
    image: str = "moqlab-pub"
    insecure: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_log_level(self) -> "PublisherDefaults":
        if self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


class SubscriberDefaults(_StrictBase):
    image: str = "moqlab-sub"
    insecure: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_log_level(self) -> "SubscriberDefaults":
        if self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


class Defaults(_StrictBase):
    relay: RelayDefaults = Field(default_factory=RelayDefaults)
    publisher: PublisherDefaults = Field(default_factory=PublisherDefaults)
    subscriber: SubscriberDefaults = Field(default_factory=SubscriberDefaults)


class StartupConfig(_StrictBase):
    relay_warmup_s: float = Field(default=2.0, ge=0)
    publisher_warmup_s: float = Field(default=1.0, ge=0)


# ── nodes ──────────────────────────────────────────────────────────────────


class RelayConfig(_StrictBase):
    listen_port: int = Field(ge=_MIN_PORT, le=_MAX_PORT)
    admin_port: int = Field(ge=_MIN_PORT, le=_MAX_PORT)
    upstream: str | None = None
    image: str | None = None
    endpoint: str | None = None
    tls: TlsConfig | None = None
    cache: CacheConfig | None = None

    @model_validator(mode="after")
    def _check(self) -> "RelayConfig":
        if self.endpoint is not None and not self.endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        if self.listen_port == self.admin_port:
            raise ValueError("listen_port and admin_port must differ")
        return self


class PublisherConfig(_StrictBase):
    connects_to: str
    namespace: str
    port: int | None = Field(default=None, ge=_MIN_PORT, le=_MAX_PORT)
    image: str | None = None
    insecure: bool | None = None
    log_level: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "PublisherConfig":
        if self.log_level is not None and self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


class SubscriberConfig(_StrictBase):
    connects_to: str
    namespace: str
    track: str
    image: str | None = None
    insecure: bool | None = None
    log_level: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "SubscriberConfig":
        if self.log_level is not None and self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


# ── links (Containernet-only shaping) ──────────────────────────────────────


class LinkSpec(_StrictBase):
    """One undirected link between two node ids with optional tc/netem shaping.

    Read by the Containernet backend only. The Docker backend silently ignores
    every link block so the same topology file works on either backend.
    """

    from_: str = Field(alias="from")
    to: str
    bandwidth_mbps: float | None = Field(default=None, gt=0)
    delay_ms: float | None = Field(default=None, ge=0)
    jitter_ms: float | None = Field(default=None, ge=0)
    loss_pct: float | None = Field(default=None, ge=0, le=100)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _check_endpoints_differ(self) -> "LinkSpec":
        if self.from_ == self.to:
            raise ValueError(f"link endpoints must differ (got {self.from_!r} twice)")
        return self

    def canonical_key(self) -> tuple[str, str]:
        """Alphabetically-ordered endpoint pair for dedup + lookup."""
        return tuple(sorted([self.from_, self.to]))  # type: ignore[return-value]


# ── top-level topology ─────────────────────────────────────────────────────


class TopologyConfig(_StrictBase):
    topology_mode: Literal["explicit"] = "explicit"
    defaults: Defaults = Field(default_factory=Defaults)
    startup: StartupConfig = Field(default_factory=StartupConfig)
    relays: dict[str, RelayConfig] = Field(default_factory=dict)
    publishers: dict[str, PublisherConfig] = Field(default_factory=dict)
    subscribers: dict[str, SubscriberConfig] = Field(default_factory=dict)
    links: list[LinkSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "TopologyConfig":
        if not self.relays:
            raise ValueError("at least one relay is required")

        # Node ids share a single namespace because every node becomes a Docker
        # container with `name=<id>` on the same network.
        all_nodes: dict[str, str] = {}
        for kind, group in (
            ("relay", self.relays),
            ("publisher", self.publishers),
            ("subscriber", self.subscribers),
        ):
            for nid in group:
                if not _NODE_ID_RE.match(nid):
                    raise ValueError(
                        f"invalid {kind} id {nid!r}: must match {_NODE_ID_RE.pattern}"
                    )
                if nid in all_nodes:
                    raise ValueError(
                        f"node id {nid!r} reused: already declared as a "
                        f"{all_nodes[nid]}"
                    )
                all_nodes[nid] = kind

        # Relay ports unique across all relays.
        seen_ports: dict[int, str] = {}
        for rid, r in self.relays.items():
            for kind, port in (("listen", r.listen_port), ("admin", r.admin_port)):
                if port in seen_ports:
                    raise ValueError(
                        f"{kind}_port {port} on relay {rid!r} collides with relay "
                        f"{seen_ports[port]!r}"
                    )
                seen_ports[port] = rid

        # Relay upstream references must resolve to known relays.
        for rid, r in self.relays.items():
            if r.upstream is None:
                continue
            if r.upstream == rid:
                raise ValueError(f"relay {rid!r} cannot list itself as upstream")
            if r.upstream not in self.relays:
                raise ValueError(
                    f"relay {rid!r} upstream {r.upstream!r} is not a known relay"
                )

        # Upstream chain has no cycle (single-upstream-per-relay → walk it).
        for start in self.relays:
            seen: set[str] = set()
            cur: str | None = start
            while cur is not None:
                if cur in seen:
                    raise ValueError(
                        f"cycle detected in upstream chain starting at {start!r}"
                    )
                seen.add(cur)
                cur = self.relays[cur].upstream

        # Pub/sub connects_to must reference a known relay.
        for pid, p in self.publishers.items():
            if p.connects_to not in self.relays:
                raise ValueError(
                    f"publisher {pid!r} connects_to {p.connects_to!r} is not a known relay"
                )
        for sid, s in self.subscribers.items():
            if s.connects_to not in self.relays:
                raise ValueError(
                    f"subscriber {sid!r} connects_to {s.connects_to!r} is not a known relay"
                )

        # Links: endpoints must be known nodes; each undirected pair appears once.
        seen_links: set[tuple[str, str]] = set()
        for link in self.links:
            for endpoint in (link.from_, link.to):
                if endpoint not in all_nodes:
                    raise ValueError(
                        f"link references unknown node {endpoint!r}"
                    )
            key = link.canonical_key()
            if key in seen_links:
                raise ValueError(
                    f"duplicate link between {key[0]!r} and {key[1]!r}"
                )
            seen_links.add(key)

        return self

    # ── helpers for downstream code ─────────────────────────────────────────

    def link_for(self, a: str, b: str) -> LinkSpec | None:
        """Return the link between two nodes, regardless of ordering, or None."""
        wanted = tuple(sorted([a, b]))
        for link in self.links:
            if link.canonical_key() == wanted:
                return link
        return None

    def relay_image(self, rid: str) -> str:
        return self.relays[rid].image or self.defaults.relay.image

    def relay_endpoint(self, rid: str) -> str:
        return self.relays[rid].endpoint or self.defaults.relay.endpoint

    def relay_tls(self, rid: str) -> TlsConfig:
        return self.relays[rid].tls or self.defaults.relay.tls

    def relay_cache(self, rid: str) -> CacheConfig:
        return self.relays[rid].cache or self.defaults.relay.cache

    def publisher_image(self, pid: str) -> str:
        return self.publishers[pid].image or self.defaults.publisher.image

    def publisher_insecure(self, pid: str) -> bool:
        p = self.publishers[pid]
        return p.insecure if p.insecure is not None else self.defaults.publisher.insecure

    def publisher_log_level(self, pid: str) -> str:
        return self.publishers[pid].log_level or self.defaults.publisher.log_level

    def subscriber_image(self, sid: str) -> str:
        return self.subscribers[sid].image or self.defaults.subscriber.image

    def subscriber_insecure(self, sid: str) -> bool:
        s = self.subscribers[sid]
        return s.insecure if s.insecure is not None else self.defaults.subscriber.insecure

    def subscriber_log_level(self, sid: str) -> str:
        return self.subscribers[sid].log_level or self.defaults.subscriber.log_level


def load_topology(path: str | Path) -> TopologyConfig:
    """Load and validate a topology YAML. Raises ConfigError on any failure."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"topology config not found: {p}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {p}: {e}") from e

    if raw is None:
        raise ConfigError(f"{p} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{p}: top-level value must be a mapping, got {type(raw).__name__}"
        )

    try:
        return TopologyConfig.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"{p}: {e}") from e
