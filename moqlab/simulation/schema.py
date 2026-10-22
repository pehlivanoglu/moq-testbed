from __future__ import annotations

from collections import deque
import math
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import ConfigError


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Workload(Model):
    kind: Literal["bulk", "paced", "segmented"] = "paced"
    rate_mbps: float = Field(default=5, gt=0)
    segment_duration_ms: float = Field(default=2000, gt=0)
    representation_sequence_mbps: list[Annotated[float, Field(gt=0)]] = Field(default_factory=lambda: [5])
    start_s: float = Field(default=0.1, ge=0)
    stop_s: float | None = Field(default=None, gt=0)


class Queue(Model):
    kind: Literal["fifo", "red", "pie", "fq_codel", "dualpi2"] = "fifo"
    limit_packets: int = Field(default=1000, ge=2, le=1000000)
    target_ms: float = Field(default=15, gt=0)
    ecn: bool = True
    red_min_packets: float = Field(default=20, gt=0)
    red_max_packets: float = Field(default=60, gt=0)
    dualpi2_step_ms: float = Field(default=1, gt=0)
    dualpi2_update_ms: float = Field(default=16, gt=0)
    dualpi2_coupling: float = Field(default=2, gt=0)

    @model_validator(mode="after")
    def check_thresholds(self) -> Queue:
        if self.red_max_packets <= self.red_min_packets:
            raise ValueError("RED maximum must exceed minimum")
        if self.kind == "red" and self.red_max_packets > self.limit_packets:
            raise ValueError("RED maximum threshold exceeds queue limit")
        if self.kind == "dualpi2" and not self.ecn:
            raise ValueError("DualPI2 requires ECN marking; choose an endpoint ECN mode separately")
        return self


class Transport(Model):
    congestion_control: Literal["cubic", "bbr"] = "cubic"
    ecn: Literal["off", "ect0", "ect1"] = "ect0"
    mtu_bytes: int = Field(default=1252, ge=1200, le=1400,
                           description="picoquic mtu_max API setting; initial packet sizing and probing follow the pinned stack")
    relay_buffer_bytes: int = Field(default=1048576, ge=65536)
    feedback_batch_ms: float = Field(default=10, gt=0, le=350)
    max_feedback_records: int = Field(default=65536, ge=128, le=1000000)


class Route(Model):
    sender: str
    receiver: str
    forward: list[str] = Field(min_length=2)
    reverse: list[str] = Field(min_length=2)


class LinkEvent(Model):
    kind: Literal["link"] = "link"
    time_s: float = Field(ge=0)
    source: str
    target: str
    bandwidth_mbps: float | None = Field(default=None, gt=0)
    delay_ms: float | None = Field(default=None, ge=0)
    jitter_ms: float | None = Field(default=None, ge=0)
    loss_pct: float | None = Field(default=None, ge=0, le=100)
    up: bool | None = None


class RouteEvent(Route):
    kind: Literal["route"] = "route"
    time_s: float = Field(ge=0)


class QueueEvent(Model):
    kind: Literal["queue"] = "queue"
    time_s: float = Field(ge=0)
    source: str
    target: str
    queue: Queue
    backlog_policy: Literal["reject", "flush"] = "reject"


class FlowEvent(Model):
    kind: Literal["flow"] = "flow"
    time_s: float = Field(ge=0)
    origin: str
    active: bool


Event = Annotated[LinkEvent | RouteEvent | QueueEvent | FlowEvent, Field(discriminator="kind")]


class Evaluation(Model):
    window_ms: float = Field(default=350, gt=0)
    utilization_threshold: float = Field(default=0.8, gt=0, le=1)
    backlog_fraction_threshold: float = Field(default=0.5, gt=0, le=1)
    sojourn_threshold_ms: float = Field(default=1, ge=0)
    bootstrap_samples: int = Field(default=2000, ge=100)
    analysis_seed: int = Field(default=20261021, ge=0)
    sensitivity_factors: list[Annotated[float, Field(gt=0, le=4)]] = Field(default_factory=lambda: [0.8, 1.0, 1.2], min_length=1)


class QuicBulk(Model):
    id: str = Field(min_length=1)
    sender: str
    receiver: str
    start_s: float = Field(default=0.1, ge=0)
    duration_s: float = Field(default=60, gt=0)


class ConnectionTiming(Model):
    start_s: float = Field(default=0, ge=0)
    stop_s: float | None = Field(default=None, gt=0)


class Experiment(Model):
    duration_s: float = Field(default=60, gt=0)
    seed: int = Field(default=1, ge=1, le=2147483647)
    repetitions: int = Field(default=1, ge=1, le=10000)
    transport: Transport = Field(default_factory=Transport)
    transport_overrides: dict[str, Transport] = Field(default_factory=dict)
    workload: Workload = Field(default_factory=Workload)
    workloads: dict[str, Workload] = Field(default_factory=dict)
    default_bandwidth_mbps: float = Field(default=1000, gt=0)
    default_delay_ms: float = Field(default=0, ge=0)
    wire_overhead_bytes: int = Field(default=38, ge=0, le=256)
    link_mtu_bytes: int = Field(default=1500, ge=1280, le=65535)
    queue: Queue = Field(default_factory=Queue)
    queues: dict[str, Queue] = Field(default_factory=dict)
    routes: list[Route] = Field(default_factory=list)
    quic_bulk: list[QuicBulk] = Field(default_factory=list)
    connections: dict[str, ConnectionTiming] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    trace: Literal["summary", "packets"] = "summary"
    pcap: bool = False
    qlog: bool = False
    evaluation: Evaluation = Field(default_factory=Evaluation)


class SimulationConfig(Model):
    kind: Literal["simulation"] = "simulation"
    schema_version: Literal[1] = 1
    topology: TopologyConfig
    experiment: Experiment = Field(default_factory=Experiment)

    @model_validator(mode="after")
    def check_experiment(self) -> SimulationConfig:
        t, e = self.topology, self.experiment
        if not t.links:
            raise ValueError("simulation requires explicit physical links")
        if len(t.links) * 2 > 65534:
            raise ValueError("physical interface address pool exhausted")
        ids = set(t.relays) | set(t.publishers) | set(t.subscribers) | set(t.routers) | set(t.switches)
        if t.traffic:
            ids.update(x.id for x in [*t.traffic.senders, *t.traffic.receivers])
        def finite(value: object) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("all simulation numbers must be finite")
            if isinstance(value, dict):
                for item in value.values():
                    finite(item)
            elif isinstance(value, list):
                for item in value:
                    finite(item)
        finite(t.model_dump(mode="json"))
        edges = {frozenset((link.from_, link.to)) for link in t.links}
        for name, relay in t.relays.items():
            if relay.sbd.algorithm != "PracticalPassiveAndRFC":
                raise ValueError(f"{name}: simulation supports PracticalPassiveAndRFC only")
            if relay.sbd.delay_source != "owd":
                raise ValueError(f"{name}: simulation requires OWD")
            cache = relay.cache or t.defaults.relay.cache
            if cache.enabled:
                raise ValueError("media cache behavior is not simulated")
        if set(e.workloads) - set(t.publishers):
            raise ValueError("workloads must reference origins (publishers)")
        if set(e.transport_overrides) - (set(t.relays) | set(t.publishers) | set(t.subscribers)):
            raise ValueError("unknown transport override node")
        if any(transport.mtu_bytes + 28 > e.link_mtu_bytes for transport in [e.transport, *e.transport_overrides.values()]):
            raise ValueError("QUIC MTU setting plus conservative IPv4/UDP headroom exceeds link MTU")
        for workload in [e.workload, *e.workloads.values()]:
            if workload.start_s >= e.duration_s or (workload.stop_s is not None and workload.stop_s <= workload.start_s):
                raise ValueError("workload start/stop outside valid interval")
        for timing in e.connections.values():
            if timing.start_s >= e.duration_s or (timing.stop_s is not None and timing.stop_s <= timing.start_s):
                raise ValueError("invalid connection start/stop interval")
        for key in e.queues:
            pair = key.split("->")
            if len(pair) != 2 or frozenset(pair) not in edges:
                raise ValueError(f"invalid directed queue key: {key}; expected source->target")
        def check_route(route: Route) -> None:
            if route.sender not in ids or route.receiver not in ids:
                raise ValueError("route endpoint is unknown")
            for path, start, end in [(route.forward, route.sender, route.receiver), (route.reverse, route.receiver, route.sender)]:
                if path[0] != start or path[-1] != end or len(set(path)) != len(path):
                    raise ValueError("route endpoints or loop are invalid")
                if any(frozenset(pair) not in edges for pair in zip(path, path[1:])):
                    raise ValueError("route must follow physical links")
                if any(node not in t.routers and node not in t.switches for node in path[1:-1]):
                    raise ValueError("route transit nodes must be routers or switches")
        pairs = set()
        for route in e.routes:
            check_route(route)
            pair = tuple(sorted((route.sender, route.receiver)))
            if pair in pairs:
                raise ValueError("duplicate endpoint route")
            pairs.add(pair)
        bulk_ids = set()
        for load in e.quic_bulk:
            if load.id in bulk_ids or load.sender not in ids or load.receiver not in ids or load.sender == load.receiver:
                raise ValueError("QUIC bulk requires unique ids and distinct known endpoints")
            if load.sender in t.switches or load.receiver in t.switches or load.start_s >= e.duration_s:
                raise ValueError("invalid QUIC bulk endpoints or start time")
            bulk_ids.add(load.id)
        for event in e.events:
            if event.time_s >= e.duration_s:
                raise ValueError("event must precede simulation end")
            if isinstance(event, RouteEvent):
                check_route(event)
            elif isinstance(event, FlowEvent):
                if event.origin not in t.publishers:
                    raise ValueError("flow event must reference an origin")
            elif frozenset((event.source, event.target)) not in edges:
                raise ValueError("event must reference a physical link")
        return self


def load_config(path: Path) -> SimulationConfig:
    try:
        raw = yaml.safe_load(path.read_text())
        if isinstance(raw, dict) and "topology" not in raw:
            raw = {"topology": raw}
        return SimulationConfig.model_validate(raw)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ConfigError(f"invalid simulation config {path}: {error}") from error


def document(config: SimulationConfig) -> dict[str, object]:
    data = config.model_dump(mode="json", by_alias=True)
    for name, subscriber in config.topology.subscribers.items():
        client = subscriber.media_client or config.topology.defaults.subscriber.media_client
        if client != "native":
            data["topology"]["subscribers"][name].pop("native_playback", None)
    return data


def shortest_path(topology: TopologyConfig, source: str, target: str) -> list[str]:
    graph: dict[str, list[str]] = {}
    for link in topology.links:
        graph.setdefault(link.from_, []).append(link.to)
        graph.setdefault(link.to, []).append(link.from_)
    todo = deque([[source]])
    seen = {source}
    while todo:
        path = todo.popleft()
        if path[-1] == target:
            return path
        if len(path) > 1 and path[-1] not in topology.routers and path[-1] not in topology.switches:
            continue
        for node in sorted(graph.get(path[-1], [])):
            if node not in seen:
                seen.add(node)
                todo.append([*path, node])
    raise ConfigError(f"no routed path: {source} -> {target}")


def resolved_config(config: SimulationConfig) -> dict[str, object]:
    data = config.model_dump(mode="json", by_alias=True)
    t, e = config.topology, config.experiment
    routes = {(r.sender, r.receiver): r.model_dump() for r in e.routes}
    applications = []
    for node, pub in t.publishers.items():
        applications.append({"id": f"{node}->{pub.connects_to}", "sender": node, "receiver": pub.connects_to, "origin": node})
    origins: dict[str, str] = {}
    for node, pub in t.publishers.items():
        if pub.connects_to in origins:
            raise ConfigError("one synthetic origin per relay tree is required")
        origins[pub.connects_to] = node
    def origin(relay: str) -> str:
        if relay in origins:
            return origins[relay]
        upstream = t.relays[relay].upstream
        if upstream is None:
            raise ConfigError(f"relay {relay} has no origin")
        origins[relay] = origin(upstream)
        return origins[relay]
    for node, relay in t.relays.items():
        if relay.upstream:
            applications.append({"id": f"{relay.upstream}->{node}", "sender": relay.upstream, "receiver": node, "origin": origin(node)})
    for node, sub in t.subscribers.items():
        applications.append({"id": f"{sub.connects_to}->{node}", "sender": sub.connects_to, "receiver": node, "origin": origin(sub.connects_to)})
    for load in e.quic_bulk:
        applications.append({"id": f"external:{load.id}", "sender": load.sender, "receiver": load.receiver,
                             "origin": f"@{load.id}", "start_s": load.start_s, "stop_s": load.start_s + load.duration_s})
    if set(e.connections) - {app["id"] for app in applications}:
        raise ConfigError("connection schedules must reference application ids (sender->receiver or external:id)")
    allowed_pairs = {(app["sender"], app["receiver"]) for app in applications}
    extra_flows = 0
    if t.traffic:
        allowed_pairs.update((route.path[0], route.path[-1]) for route in t.traffic.routes.values())
        extra_flows = sum(getattr(flow, "connections", getattr(flow, "clients", 1)) for flow in t.traffic.flows)
    if 2 * (len(applications) + extra_flows) > 65534:
        raise ConfigError("connection address pool exhausted")
    for event in e.events:
        if isinstance(event, RouteEvent) and (event.sender, event.receiver) not in allowed_pairs:
            raise ConfigError("route event does not target any configured application or traffic flow")
    for app in applications:
        sender, receiver = app["sender"], app["receiver"]
        if (sender, receiver) not in routes:
            reverse = routes.get((receiver, sender))
            routes[sender, receiver] = {"sender": sender, "receiver": receiver,
                "forward": reverse["reverse"] if reverse else shortest_path(t, sender, receiver),
                "reverse": reverse["forward"] if reverse else shortest_path(t, receiver, sender)}
    data["applications"] = applications
    data["experiment"]["routes"] = list(routes.values())
    return data
