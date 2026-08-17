"""Pydantic v2 schema for the moqlab topology config.

A topology is a YAML file with node-bearing blocks (relays, publishers,
subscribers, routers, traffic) plus `links:` physical wiring and inheritable defaults.
Every node id is unique across the whole topology — it becomes a Docker
container name and the name other nodes use to reach it.

Schema invariants enforced here:
  - node ids globally unique across all node kinds
  - upstream / connects_to references resolve to a known relay (never a router)
  - all relay listen_port + admin_port values unique (host-port pool)
  - single upstream per relay, no cycles in the upstream chain
  - links reference known nodes, each undirected pair appears at most once
  - when links/routers are declared, every application edge (each
    upstream / connects_to pair) must have a path through the link graph
  - per-direction `aqm` only where the egress node is a router (endpoint
    images ship an iproute2 too old for modern AQMs)
  - every declared router appears in at least one link
  - generative mode rejected (v1 explicit only)

`links:`, `routers:`, and `traffic:` describe routed experiments read by the
Containernet backend only. The Docker backend (flat bridge, no forwarding
nodes) ignores `links:` and refuses topologies that declare routers or traffic.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from moqlab.exceptions import ConfigError

_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
_MIN_PORT = 1024
_MAX_PORT = 65535
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_SAFE_ASSET_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class _StrictBase(BaseModel):
    """Reject unknown fields so typos surface as ConfigError, not silent drops."""

    model_config = ConfigDict(extra="forbid", frozen=False)


# ── leaf config blocks ─────────────────────────────────────────────────────


class TlsConfig(_StrictBase):
    insecure: bool = True
    generated: bool = False
    cert_file: str | None = None
    key_file: str | None = None
    ca_cert: str | None = None

    @model_validator(mode="after")
    def _check_cert_pair(self) -> "TlsConfig":
        if self.generated:
            if self.insecure:
                raise ValueError("tls.generated=true requires insecure=false")
            if self.cert_file or self.key_file:
                raise ValueError("generated TLS cannot set cert_file or key_file")
        elif not self.insecure and not (self.cert_file and self.key_file):
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
    media_image: str = "moqlab-media-pub"
    insecure: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_log_level(self) -> "PublisherDefaults":
        if self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


class SubscriberDefaults(_StrictBase):
    image: str = "moqlab-sub"
    media_image: str = "moqlab-media-sub"
    native_media_image: str = "moqlab-media-native-sub"
    media_client: Literal["chrome", "native"] = "chrome"
    native_playback: Literal["receive", "simulate"] = "receive"
    insecure: bool = True
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_log_level(self) -> "SubscriberDefaults":
        if self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return self


class RouterDefaults(_StrictBase):
    image: str = "moqlab-router"


class TrafficDefaults(_StrictBase):
    image: str = "moqlab-traffic"
    tcp_port: int = Field(default=9000, ge=_MIN_PORT, le=_MAX_PORT)
    udp_port: int = Field(default=9001, ge=_MIN_PORT, le=_MAX_PORT)

    @model_validator(mode="after")
    def _check_ports_differ(self) -> "TrafficDefaults":
        if self.tcp_port == self.udp_port:
            raise ValueError("traffic tcp_port and udp_port must differ")
        return self


class StartupConfig(_StrictBase):
    relay_warmup_s: float = Field(default=2.0, ge=0)
    publisher_warmup_s: float = Field(default=1.0, ge=0)
    media_ready_timeout_s: float = Field(default=30.0, gt=0)
    traffic_ready_timeout_s: float = Field(default=5.0, gt=0)


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
    kind: Literal["text", "media"] = "text"
    connects_to: str
    namespace: str | None = None
    port: int | None = Field(default=None, ge=_MIN_PORT, le=_MAX_PORT)
    asset: str | None = None
    listen_port: int | None = Field(default=None, ge=_MIN_PORT, le=_MAX_PORT)
    fingerprint_port: int | None = Field(default=None, ge=_MIN_PORT, le=_MAX_PORT)
    image: str | None = None
    insecure: bool | None = None
    log_level: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "PublisherConfig":
        if self.log_level is not None and self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        if self.kind == "text":
            if not self.namespace:
                raise ValueError("text publisher requires namespace")
            if (
                self.asset is not None
                or self.listen_port is not None
                or self.fingerprint_port is not None
            ):
                raise ValueError("text publisher cannot set media fields")
        else:
            if not self.asset or not _SAFE_ASSET_RE.fullmatch(self.asset):
                raise ValueError("media publisher asset must be a safe file name")
            if self.listen_port is None or self.fingerprint_port is None:
                raise ValueError("media publisher requires listen_port and fingerprint_port")
            if self.listen_port == self.fingerprint_port:
                raise ValueError("listen_port and fingerprint_port must differ")
            if self.namespace is not None or self.port is not None:
                raise ValueError("media publisher cannot set text fields")
        return self


class RouterConfig(_StrictBase):
    """An IP-forwarding node that owns link queues (AQM/ECN); runs no MoQ binary."""

    image: str | None = None


class TrafficEndpointConfig(_StrictBase):
    id: str
    image: str | None = None


class TrafficRouteConfig(_StrictBase):
    path: list[str] = Field(min_length=3)


class _TrafficFlowBase(_StrictBase):
    id: str
    route: str
    start_s: float = Field(default=0, ge=0)
    duration_s: float = Field(gt=0)


class BulkTrafficFlow(_TrafficFlowBase):
    kind: Literal["bulk"]
    connections: int = Field(default=1, gt=0, le=1024)
    chunk_bytes: int = Field(default=65536, ge=1024, le=65536)


class CbrTrafficFlow(_TrafficFlowBase):
    kind: Literal["cbr"]
    rate_mbps: float = Field(gt=0)
    packet_size_bytes: int = Field(default=1200, ge=64, le=65507)


class SegmentedTrafficFlow(_TrafficFlowBase):
    kind: Literal["segmented"]
    clients: int = Field(default=1, gt=0, le=1024)
    segment_duration_ms: int = Field(default=2000, gt=0)
    representation_sequence_mbps: list[Annotated[float, Field(gt=0)]] = Field(
        min_length=1
    )


TrafficFlow = Annotated[
    BulkTrafficFlow | CbrTrafficFlow | SegmentedTrafficFlow,
    Field(discriminator="kind"),
]


class TrafficConfig(_StrictBase):
    sender: TrafficEndpointConfig
    receiver: TrafficEndpointConfig
    routes: dict[str, TrafficRouteConfig] = Field(min_length=1, max_length=254)
    flows: list[TrafficFlow] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_local_references(self) -> "TrafficConfig":
        if self.sender.id == self.receiver.id:
            raise ValueError("traffic sender and receiver ids must differ")
        for label, value in (
            ("traffic sender id", self.sender.id),
            ("traffic receiver id", self.receiver.id),
            *(("traffic route name", name) for name in self.routes),
        ):
            if not _NODE_ID_RE.fullmatch(value):
                raise ValueError(f"{label} {value!r} must match {_NODE_ID_RE.pattern}")
        seen: set[str] = set()
        for flow in self.flows:
            if not _NODE_ID_RE.fullmatch(flow.id):
                raise ValueError(
                    f"traffic flow id {flow.id!r} must match {_NODE_ID_RE.pattern}"
                )
            if flow.id in seen:
                raise ValueError(f"duplicate traffic flow id {flow.id!r}")
            seen.add(flow.id)
            if flow.route not in self.routes:
                raise ValueError(
                    f"traffic flow {flow.id!r} references unknown route {flow.route!r}"
                )
        return self


class SubscriberConfig(_StrictBase):
    kind: Literal["text", "media"] = "text"
    connects_to: str
    namespace: str
    track: str
    image: str | None = None
    insecure: bool | None = None
    log_level: str | None = None
    media_client: Literal["chrome", "native"] | None = None
    native_playback: Literal["receive", "simulate"] | None = None
    browser_mode: Literal["headless", "x11"] | None = None
    minimal_buffer_ms: int | None = Field(default=None, ge=0)
    target_latency_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "SubscriberConfig":
        if self.log_level is not None and self.log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        if self.kind == "text":
            if any(value is not None for value in (
                self.media_client, self.native_playback, self.browser_mode,
                self.minimal_buffer_ms, self.target_latency_ms,
            )):
                raise ValueError("text subscriber cannot set media fields")
        return self


# ── links (Containernet-only shaping) ──────────────────────────────────────


class AqmKind(str, Enum):
    """AQM qdiscs moqlab can synthesize tc commands for (see orchestrator/shaping.py)."""

    dualpi2 = "dualpi2"


class DirectionSpec(_StrictBase):
    """Shaping for one direction of a link: the egress qdisc chain on one interface."""

    bandwidth_mbps: float | None = Field(default=None, gt=0)
    delay_ms: float | None = Field(default=None, ge=0)
    jitter_ms: float | None = Field(default=None, ge=0)
    loss_pct: float | None = Field(default=None, ge=0, le=100)
    aqm: AqmKind | None = None

    @model_validator(mode="after")
    def _check_jitter_requires_delay(self) -> "DirectionSpec":
        if self.jitter_ms is not None and self.delay_ms is None:
            raise ValueError(
                "jitter_ms requires delay_ms (netem syntax: delay <ms> <jitter>)"
            )
        return self

    def is_noop(self) -> bool:
        return (
            self.bandwidth_mbps is None
            and self.delay_ms is None
            and self.jitter_ms is None
            and self.loss_pct is None
            and self.aqm is None
        )

    def merged_over(self, base: "DirectionSpec") -> "DirectionSpec":
        """This spec's explicitly-set fields laid over `base`.

        Per-field, not per-block: a link that sets only `bandwidth_mbps` still
        inherits `delay_ms` from the default. `model_fields_set` — not a
        truthiness or None test — decides what "explicitly set" means, so
        `delay_ms: 0` overrides a nonzero default and an explicit `aqm: null`
        clears an inherited one.
        """
        merged = base.model_dump()
        merged.update(self.model_dump(include=self.model_fields_set))
        return DirectionSpec.model_validate(merged)


class LinkDefaults(_StrictBase):
    """Per-direction shaping inherited by every link that does not override it."""

    forward: DirectionSpec = Field(default_factory=DirectionSpec)
    reverse: DirectionSpec = Field(default_factory=DirectionSpec)


class Defaults(_StrictBase):
    relay: RelayDefaults = Field(default_factory=RelayDefaults)
    publisher: PublisherDefaults = Field(default_factory=PublisherDefaults)
    subscriber: SubscriberDefaults = Field(default_factory=SubscriberDefaults)
    router: RouterDefaults = Field(default_factory=RouterDefaults)
    traffic: TrafficDefaults = Field(default_factory=TrafficDefaults)
    link: LinkDefaults = Field(default_factory=LinkDefaults)


class LinkSpec(_StrictBase):
    """One link (veth pair) between two node ids with per-direction shaping.

    `forward` shapes from→to traffic (egress qdisc chain on from's interface);
    `reverse` shapes to→from traffic (egress qdisc chain on to's interface).
    Read by the Containernet backend only.
    """

    from_: str = Field(alias="from")
    to: str
    forward: DirectionSpec = Field(default_factory=DirectionSpec)
    reverse: DirectionSpec = Field(default_factory=DirectionSpec)

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
    routers: dict[str, RouterConfig] = Field(default_factory=dict)
    traffic: TrafficConfig | None = None
    links: list[LinkSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolve_link_defaults(self) -> "TopologyConfig":
        """Fold `defaults.link` into every link before any other check runs.

        Done here rather than in an accessor so `link.forward` / `link.reverse`
        are the effective specs for every consumer — the shaping backend, the
        visualizer, and the aqm-egress rule in `_check` below, which must see a
        default-supplied aqm to police it. Validators declared with mode="after"
        run in definition order, so this precedes `_check`.
        """
        for link in self.links:
            link.forward = link.forward.merged_over(self.defaults.link.forward)
            link.reverse = link.reverse.merged_over(self.defaults.link.reverse)
        return self

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
            ("router", self.routers),
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

        if self.traffic is not None:
            for kind, endpoint in (
                ("traffic sender", self.traffic.sender),
                ("traffic receiver", self.traffic.receiver),
            ):
                nid = endpoint.id
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

        media_publishers = {
            pid: publisher
            for pid, publisher in self.publishers.items()
            if publisher.kind == "media"
        }
        media_subscribers = {
            sid: subscriber
            for sid, subscriber in self.subscribers.items()
            if subscriber.kind == "media"
        }
        if media_subscribers and not media_publishers:
            raise ValueError("media subscriber requires a media publisher")
        if media_publishers or media_subscribers:
            for sid, subscriber in media_subscribers.items():
                client = self.subscriber_media_client(sid)
                if client == "native":
                    if subscriber.browser_mode is not None:
                        raise ValueError(
                            "native media subscriber cannot set browser fields"
                        )
                    if self.subscriber_native_playback(sid) == "receive":
                        if any(value is not None for value in (
                            subscriber.minimal_buffer_ms,
                            subscriber.target_latency_ms,
                        )):
                            raise ValueError(
                                "native receive subscriber cannot set playback buffer fields"
                            )
                        continue
                    subscriber.minimal_buffer_ms = (
                        200
                        if subscriber.minimal_buffer_ms is None
                        else subscriber.minimal_buffer_ms
                    )
                    subscriber.target_latency_ms = (
                        300
                        if subscriber.target_latency_ms is None
                        else subscriber.target_latency_ms
                    )
                    if subscriber.target_latency_ms <= subscriber.minimal_buffer_ms:
                        raise ValueError("target_latency_ms must exceed minimal_buffer_ms")
                    continue
                if "native_playback" in subscriber.model_fields_set:
                    raise ValueError(
                        "chrome media subscriber cannot set native_playback"
                    )
                subscriber.browser_mode = subscriber.browser_mode or "headless"
                subscriber.minimal_buffer_ms = (
                    200
                    if subscriber.minimal_buffer_ms is None
                    else subscriber.minimal_buffer_ms
                )
                subscriber.target_latency_ms = (
                    300
                    if subscriber.target_latency_ms is None
                    else subscriber.target_latency_ms
                )
                if subscriber.target_latency_ms <= subscriber.minimal_buffer_ms:
                    raise ValueError("target_latency_ms must exceed minimal_buffer_ms")

            for rid in self.relays:
                if not self.relay_tls(rid).generated:
                    raise ValueError(
                        f"media topology requires generated TLS on relay {rid!r}"
                    )

            def _relay_root(rid: str) -> str:
                while self.relays[rid].upstream is not None:
                    rid = self.relays[rid].upstream  # type: ignore[assignment]
                return rid

            origins_by_root: dict[str, str] = {}
            for media_pid, media_publisher in media_publishers.items():
                media_root = media_publisher.connects_to
                if self.relays[media_root].upstream is not None:
                    raise ValueError(
                        f"media publisher {media_pid!r} must attach to a root relay"
                    )
                if media_root in origins_by_root:
                    raise ValueError("v1 supports one media publisher per relay tree")
                origins_by_root[media_root] = media_pid
            for sid, subscriber in media_subscribers.items():
                if _relay_root(subscriber.connects_to) not in origins_by_root:
                    raise ValueError(
                        f"media subscriber {sid!r} is not in the media publisher tree"
                    )
        # Links are the physical wiring: endpoints must exist, each undirected
        # pair appears once, and `aqm` may only sit on a router's egress
        # because endpoint images ship an iproute2 too old for modern AQMs.
        seen_links: set[tuple[str, str]] = set()
        linked_nodes: set[str] = set()
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
            linked_nodes.update(key)
            if link.forward.aqm is not None and link.from_ not in self.routers:
                raise ValueError(
                    f"link {link.from_!r}->{link.to!r}: forward.aqm requires "
                    f"the egress node {link.from_!r} to be a router"
                )
            if link.reverse.aqm is not None and link.to not in self.routers:
                raise ValueError(
                    f"link {link.from_!r}->{link.to!r}: reverse.aqm requires "
                    f"the egress node {link.to!r} to be a router"
                )

        for rid in self.routers:
            if rid not in linked_nodes:
                raise ValueError(f"router {rid!r} does not appear in any link")

        if self.traffic is not None:
            sender = self.traffic.sender.id
            receiver = self.traffic.receiver.id
            link_keys = {link.canonical_key() for link in self.links}
            for name, route in self.traffic.routes.items():
                if route.path[0] != sender or route.path[-1] != receiver:
                    raise ValueError(
                        f"traffic route {name!r} must start at {sender!r} and end at "
                        f"{receiver!r}"
                    )
                if len(set(route.path)) != len(route.path):
                    raise ValueError(f"traffic route {name!r} repeats a node")
                for intermediate in route.path[1:-1]:
                    if intermediate not in self.routers:
                        raise ValueError(
                            f"traffic route {name!r} intermediate {intermediate!r} "
                            "must be a router"
                        )
                for a, b in zip(route.path, route.path[1:]):
                    if tuple(sorted((a, b))) not in link_keys:
                        raise ValueError(
                            f"traffic route {name!r} uses undeclared link {a!r}-{b!r}"
                        )

        # Every application edge (upstream / connects_to pair) must be
        # realizable as a path through the link graph. Skipped when neither
        # links nor routers are declared: Docker-backend configs need no
        # wiring, and the Containernet backend separately refuses to run
        # without links.
        if self.links or self.routers:
            app_edges: set[tuple[str, str]] = set()
            for rid, relay in self.relays.items():
                if relay.upstream is not None:
                    app_edges.add(tuple(sorted([rid, relay.upstream])))  # type: ignore[arg-type]
            for pid, publisher in self.publishers.items():
                app_edges.add(tuple(sorted([pid, publisher.connects_to])))  # type: ignore[arg-type]
            for sid, subscriber in self.subscribers.items():
                app_edges.add(tuple(sorted([sid, subscriber.connects_to])))  # type: ignore[arg-type]

            component: dict[str, str] = {nid: nid for nid in all_nodes}

            def _root(nid: str) -> str:
                while component[nid] != nid:
                    component[nid] = component[component[nid]]
                    nid = component[nid]
                return nid

            for link in self.links:
                component[_root(link.from_)] = _root(link.to)

            for a, b in sorted(app_edges):
                if _root(a) != _root(b):
                    raise ValueError(
                        f"no path of links connects {a!r} and {b!r}; this "
                        "topology declares links/routers, so every "
                        "upstream/connects_to pair must be reachable through "
                        "the link graph"
                    )

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
        publisher = self.publishers[pid]
        default = (
            self.defaults.publisher.media_image
            if publisher.kind == "media"
            else self.defaults.publisher.image
        )
        return publisher.image or default

    def publisher_insecure(self, pid: str) -> bool:
        p = self.publishers[pid]
        return p.insecure if p.insecure is not None else self.defaults.publisher.insecure

    def publisher_log_level(self, pid: str) -> str:
        return self.publishers[pid].log_level or self.defaults.publisher.log_level

    def subscriber_image(self, sid: str) -> str:
        subscriber = self.subscribers[sid]
        if subscriber.kind == "media":
            default = (
                self.defaults.subscriber.native_media_image
                if self.subscriber_media_client(sid) == "native"
                else self.defaults.subscriber.media_image
            )
        else:
            default = self.defaults.subscriber.image
        return subscriber.image or default

    def subscriber_media_client(self, sid: str) -> Literal["chrome", "native"]:
        subscriber = self.subscribers[sid]
        return subscriber.media_client or self.defaults.subscriber.media_client

    def subscriber_native_playback(self, sid: str) -> Literal["receive", "simulate"]:
        subscriber = self.subscribers[sid]
        return subscriber.native_playback or self.defaults.subscriber.native_playback

    def subscriber_insecure(self, sid: str) -> bool:
        s = self.subscribers[sid]
        return s.insecure if s.insecure is not None else self.defaults.subscriber.insecure

    def subscriber_log_level(self, sid: str) -> str:
        return self.subscribers[sid].log_level or self.defaults.subscriber.log_level

    def router_image(self, rid: str) -> str:
        return self.routers[rid].image or self.defaults.router.image

    def traffic_image(self, node_id: str) -> str:
        if self.traffic is None:
            raise KeyError("topology has no traffic endpoints")
        for endpoint in (self.traffic.sender, self.traffic.receiver):
            if endpoint.id == node_id:
                return endpoint.image or self.defaults.traffic.image
        raise KeyError(node_id)

    def relay_root(self, rid: str) -> str:
        while self.relays[rid].upstream is not None:
            rid = self.relays[rid].upstream  # type: ignore[assignment]
        return rid

    def media_publisher_for_relay(self, rid: str) -> tuple[str, PublisherConfig]:
        root = self.relay_root(rid)
        for pid, publisher in self.publishers.items():
            if publisher.kind == "media" and publisher.connects_to == root:
                return pid, publisher
        raise KeyError(f"no media publisher for relay {rid!r}")


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
