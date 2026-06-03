"""Containernet backend.

Runs the same `TopologyConfig` as the Docker backend, but as Mininet hosts
inside Containernet, with one switch per link so each leg can be shaped
independently via `TCLink`. After everything is up, drops into the Containernet
`CLI(net)` shell. Exiting the shell tears the topology down.

This module imports mininet/containernet lazily so the rest of the package
remains usable on a developer laptop that doesn't have Containernet installed
(the Docker backend has no such dep). Import-time failure surfaces only when
the user actually runs `moqlab run --backend containernet`.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from moqlab.config.schema import LinkSpec, TopologyConfig, load_topology
from moqlab.config.synth import (
    synthesize_publisher_command,
    synthesize_relay_configs,
    synthesize_subscriber_command,
)
from moqlab.exceptions import OrchestratorError
from moqlab.runtime import default_run_id, default_runs_dir, relay_order, topology_edges

_log = logging.getLogger(__name__)

# Paths to the binaries inside each node image. Match the ENTRYPOINT in
# moqlab/docker/Dockerfile.{relay,pub,sub}. Containernet wipes the image
# ENTRYPOINT at container-create time AND never invokes Docker.start() during
# net.start(), so we have to launch every binary explicitly via `host.cmd()`
# after `net.start()`.
_RELAY_BINARY = "/usr/local/bin/moqx"
_RELAY_CONFIG_PATH = "/etc/moqx/relay.yaml"
_PUB_BINARY = "/usr/local/bin/moqdateserver"
_SUB_BINARY = "/usr/local/bin/moqtextclient"

# We carve one /24 out of this /16 per topology edge. Avoid Containernet's
# default 10.0.0.0/8 pool so our explicit subnets don't collide with anything
# Containernet auto-assigns.
_LINK_SUBNET_POOL = "10.20.0.0/16"


@dataclass
class ContainernetRunRecord:
    run_id: str
    run_dir: Path
    relays: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    subscribers: list[str] = field(default_factory=list)
    # (a, b) → (a's IP on link, b's IP on link). Used to write /etc/hosts.
    edge_ips: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    # node_id → ordered list of (iface_name, "x.x.x.x/24"). Used to re-apply
    # IPs after net.start(), since Containernet sometimes loses them.
    node_iface_ips: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _import_mininet():
    """Import mininet/containernet lazily and translate failures to OrchestratorError."""
    import sys

    # Import order matters: `mininet.net` first so it loads the submodules in
    # the order Containernet expects. Importing `mininet.link` before
    # `mininet.net` triggers a circular import between `mininet.link` and
    # `mininet.node` in Containernet's fork and crashes with
    # "cannot import name 'Link' from partially initialized module".
    try:
        from mininet.net import Containernet
        from mininet.node import OVSBridge
        from mininet.link import TCLink
        from mininet.cli import CLI
        from mininet.log import info, setLogLevel
    except ImportError as e:
        raise OrchestratorError(
            f"Containernet is not importable from {sys.executable!r}: {e!s}. "
            "Verify you're invoking the Python from the Containernet venv "
            "(see moqlab/README.md 'Running it')."
        ) from e

    # Mininet's CLI.__init__ sets `self.identchars` to letters + digits + `_`
    # + `.` (see mininet/cli.py:65) AFTER super().__init__(), so a class-level
    # override is shadowed. The override has to happen between that line and
    # the cmd loop — which is exactly when `run()` is called. Extend identchars
    # there so hyphenated node ids like `relay-b` parse as one token.
    class _LenientCLI(CLI):  # type: ignore[misc, valid-type]
        def run(self):
            if "-" not in self.identchars:
                self.identchars = self.identchars + "-"
            super().run()

    return _LenientCLI, TCLink, info, setLogLevel, Containernet, OVSBridge


def _tclink_kwargs(spec: LinkSpec | None) -> dict[str, object]:
    """Translate a LinkSpec into TCLink kwargs. None / missing values are dropped."""
    if spec is None:
        return {}
    kw: dict[str, object] = {}
    if spec.bandwidth_mbps is not None:
        kw["bw"] = spec.bandwidth_mbps
    if spec.delay_ms is not None:
        kw["delay"] = f"{spec.delay_ms}ms"
    if spec.jitter_ms is not None:
        kw["jitter"] = f"{spec.jitter_ms}ms"
    if spec.loss_pct is not None:
        kw["loss"] = spec.loss_pct
    return kw


class ContainernetBackend:
    def __init__(self, runs_dir: str | Path | None = None) -> None:
        self._runs_root = Path(runs_dir) if runs_dir else default_runs_dir()
        self._runs_root.mkdir(parents=True, exist_ok=True)

    def up(
        self,
        config_path: str | Path,
        run_id: str | None = None,
    ) -> ContainernetRunRecord:
        topology = load_topology(config_path)
        run_id = run_id or default_run_id()

        CLI, TCLink, info, setLogLevel, Containernet, OVSBridge = _import_mininet()
        setLogLevel("info")

        run_dir = self._runs_root / run_id
        if run_dir.exists():
            raise OrchestratorError(
                f"run dir {run_dir} already exists; choose a different --run-id"
            )
        run_dir.mkdir(parents=True)
        shutil.copyfile(config_path, run_dir / "topology.yaml")
        relay_yaml_paths = synthesize_relay_configs(topology, run_dir / "configs")

        # Containernet names every Docker host `mn.<id>`. Stale copies from a
        # previous crashed run will collide on container create; remove them
        # before we start.
        _remove_stale_mn_containers(topology, info)

        net = Containernet(switch=OVSBridge, link=TCLink)
        record = ContainernetRunRecord(run_id=run_id, run_dir=run_dir)

        try:
            self._build(net, topology, relay_yaml_paths, record, info, TCLink)

            # /etc/hosts must be in place before we launch node binaries.
            # The containers are already running at this point (addDocker
            # called dcli.start()), but Mininet's host.cmd() machinery is not
            # ready until net.start(), so direct docker exec is the earliest
            # reliable way to append neighbor names.
            info("*** Populating /etc/hosts on every node\n")
            _write_etc_hosts_via_docker(record, info)

            info("*** Starting network\n")
            net.start()

            # Mininet brings switch ports up; host veths still need an
            # explicit kick AND a re-application of our chosen IPs
            # (Containernet sometimes drops the IP during start).
            info("*** Bringing host-side interfaces up and re-applying IPs\n")
            for nid, iface_ips in record.node_iface_ips.items():
                node = net.get(nid)
                for iface, ip_cidr in iface_ips:
                    node.cmd(f"ip link set {iface} up")
                    node.cmd(f"ip addr replace {ip_cidr} dev {iface}")

            self._launch_node_binaries(net, topology, record, info)

            info(f"\n*** Containernet run_id={run_id}\n")
            info("***   Try: <node> ping <other_node>\n")
            info("***   Try: <node> tail -f /tmp/<node>.log     (pub/sub stdout)\n")
            info("***   Exit the CLI to tear the topology down.\n\n")
            CLI(net)
        finally:
            info("*** Stopping network\n")
            try:
                net.stop()
            except Exception as e:  # net.stop() can throw on partial state
                _log.warning("net.stop() raised during teardown: %s", e)

        return record

    def _build(
        self,
        net,
        topology: TopologyConfig,
        relay_yaml_paths: dict[str, Path],
        record: ContainernetRunRecord,
        info,
        TCLink,
    ) -> None:
        """Add every node, switch, and link to `net`. Pure construction; no start()."""
        nodes: dict[str, object] = {}
        info(f"*** moqlab containernet run_id={record.run_id}\n")

        # We assign IPs ourselves via addLink(params1=...) so that multi-link
        # nodes (relays in the middle of a chain) get one IP per interface.
        # Don't pass `ip=` to addDocker — Containernet's auto-assigned default
        # would land on the wrong interface and confuse routing.
        for rid in relay_order(topology):
            cfg = relay_yaml_paths[rid].resolve()
            nodes[rid] = net.addDocker(
                rid,
                dimage=topology.relay_image(rid),
                volumes=[f"{cfg}:/etc/moqx/relay.yaml:ro"],
            )
            record.relays.append(rid)

        # No `dcmd` for publishers/subscribers: Containernet wipes the image
        # ENTRYPOINT at container create time, so passing argv flags here
        # makes Docker try to exec the first flag as a binary. Instead we
        # spawn the binary explicitly in `_launch_node_binaries` after
        # net.start().
        for pid in topology.publishers:
            nodes[pid] = net.addDocker(pid, dimage=topology.publisher_image(pid))
            record.publishers.append(pid)

        for sid in topology.subscribers:
            nodes[sid] = net.addDocker(sid, dimage=topology.subscriber_image(sid))
            record.subscribers.append(sid)

        # Each edge gets its own /24. The two endpoints take .1 and .2.
        # Per-edge subnets are required because every switch is its own
        # isolated L2 segment (we add one switch per edge for per-leg
        # shaping). A single 10.0.0.0/8 would only let neighbors directly
        # connected to the same switch reach each other.
        subnet_iter = ipaddress.ip_network(_LINK_SUBNET_POOL).subnets(new_prefix=24)
        link_counter: dict[str, int] = {nid: 0 for nid in nodes}

        for idx, (a, b) in enumerate(self._derive_edges(topology), start=1):
            sub = next(subnet_iter)
            a_ip = f"{sub.network_address + 1}/{sub.prefixlen}"
            b_ip = f"{sub.network_address + 2}/{sub.prefixlen}"

            sw = net.addSwitch(f"s{idx}", failMode="standalone")
            spec = topology.link_for(a, b)
            link_kw = _tclink_kwargs(spec)

            # Each addLink creates one veth pair (host ↔ switch). The host
            # side of that veth is named "<node>-eth<N>" where N counts
            # addLink calls per node in declaration order.
            #
            # Shaping (bw / delay / loss / jitter) is applied to the FIRST
            # veth only. TCLink shapes per-veth, so if we also shaped the
            # second one, a configured `delay_ms: 5` would manifest as 10ms
            # one-way (5+5) — surprising. The current scheme makes
            # `delay_ms: X` mean "X ms one-way for this edge".
            a_iface = f"{a}-eth{link_counter[a]}"
            net.addLink(nodes[a], sw, cls=TCLink, params1={"ip": a_ip}, **link_kw)
            link_counter[a] += 1

            b_iface = f"{b}-eth{link_counter[b]}"
            net.addLink(nodes[b], sw, cls=TCLink, params1={"ip": b_ip})
            link_counter[b] += 1

            record.edge_ips[(a, b)] = (
                str(sub.network_address + 1),
                str(sub.network_address + 2),
            )
            record.node_iface_ips.setdefault(a, []).append((a_iface, a_ip))
            record.node_iface_ips.setdefault(b, []).append((b_iface, b_ip))

    @staticmethod
    def _launch_node_binaries(
        net, topology: TopologyConfig, record: ContainernetRunRecord, info
    ) -> None:
        """Start moqx / moqdateserver / moqtextclient inside each container.

        Containernet wipes the image ENTRYPOINT at container create time AND
        does NOT invoke Docker.start() during net.start() (despite the
        misleading comment in containernet/mininet/node.py:Docker.start),
        so nothing in the image is auto-launched. We launch every node's
        binary explicitly here, after `net.start()` and the iface bring-up.

        Order: relays upstream-first → configured pause → publishers →
        configured pause → subscribers. This gives moqx time to bind and gives
        publishers time to announce namespaces before subscribers ask for them.

        stdout/stderr go to `/tmp/<node>.log` inside each container; tail
        from the Mininet CLI with `<node> tail -f /tmp/<node>.log`.
        """
        # Order relays so origins (no upstream) boot first, mirroring how the
        # Docker backend does it.
        for rid in relay_order(topology):
            cmd_line = f"{_RELAY_BINARY} --config {_RELAY_CONFIG_PATH}"
            info(f"*** launching relay {rid}: {cmd_line}\n")
            net.get(rid).cmd(f"{cmd_line} > /tmp/{rid}.log 2>&1 &")

        _sleep_if_configured(
            topology.startup.relay_warmup_s,
            "for relays to bind listeners",
            info,
        )

        for pid in record.publishers:
            argv = synthesize_publisher_command(topology, pid)
            cmd_line = " ".join([_PUB_BINARY] + _shell_quote_argv(argv))
            info(f"*** launching publisher {pid}: {cmd_line}\n")
            net.get(pid).cmd(f"{cmd_line} > /tmp/{pid}.log 2>&1 &")

        if record.publishers and record.subscribers:
            _sleep_if_configured(
                topology.startup.publisher_warmup_s,
                "for publishers to announce namespaces",
                info,
            )

        for sid in record.subscribers:
            argv = synthesize_subscriber_command(topology, sid)
            cmd_line = " ".join([_SUB_BINARY] + _shell_quote_argv(argv))
            info(f"*** launching subscriber {sid}: {cmd_line}\n")
            net.get(sid).cmd(f"{cmd_line} > /tmp/{sid}.log 2>&1 &")

    @staticmethod
    def _derive_edges(topology: TopologyConfig) -> list[tuple[str, str]]:
        return topology_edges(topology)


def _shell_quote_argv(argv: list[str]) -> list[str]:
    """Shell-quote each argv element so dcmd survives shell interpretation."""
    import shlex

    return [shlex.quote(a) for a in argv]


def _sleep_if_configured(seconds: float, reason: str, info) -> None:
    if seconds <= 0:
        return
    info(f"*** waiting {seconds:g}s {reason}\n")
    time.sleep(seconds)


def _write_etc_hosts_via_docker(record: ContainernetRunRecord, info) -> None:
    """Append per-neighbor /etc/hosts entries on every node, via docker exec.

    Runs before net.start() so all nodes can resolve their direct neighbors
    before `_launch_node_binaries()` starts moqx / moxygen. We can't use
    Mininet's `host.cmd()` yet — its shell-in-ns machinery only comes up at
    net.start(). Containers themselves are running (Containernet's addDocker
    creates and starts them), so direct docker exec works.
    """
    try:
        import docker
    except ImportError:
        return
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        return

    per_node: dict[str, list[tuple[str, str]]] = {}
    for (a, b), (a_ip, b_ip) in record.edge_ips.items():
        per_node.setdefault(a, []).append((b, b_ip))
        per_node.setdefault(b, []).append((a, a_ip))

    for nid, neighbors in per_node.items():
        try:
            container = client.containers.get(f"mn.{nid}")
        except docker.errors.NotFound:
            info(f"*** /etc/hosts: container mn.{nid} not found, skipping\n")
            continue
        for neighbor_id, neighbor_ip in neighbors:
            # `>>` rather than `>` to keep Docker's own /etc/hosts entries.
            cmd = f"echo '{neighbor_ip} {neighbor_id}' >> /etc/hosts"
            container.exec_run(["sh", "-c", cmd])


def _remove_stale_mn_containers(topology: TopologyConfig, info) -> None:
    """Force-remove any `mn.<id>` containers from previous failed runs.

    Containernet's `addDocker` creates containers named `mn.<id>`. If a
    previous run crashed before `net.stop()` they linger and the next run
    fails with a 409 Conflict on container create. We pre-emptively delete
    any that match the node ids in this topology.
    """
    try:
        import docker  # already a moqlab dep
        from docker.errors import NotFound
    except ImportError:
        return

    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        return  # if the daemon is unreachable, let Containernet fail with its own error

    expected = (
        [f"mn.{rid}" for rid in topology.relays]
        + [f"mn.{pid}" for pid in topology.publishers]
        + [f"mn.{sid}" for sid in topology.subscribers]
    )
    for name in expected:
        try:
            container = client.containers.get(name)
        except NotFound:
            continue
        info(f"*** removing stale container {name}\n")
        try:
            container.remove(force=True)
        except Exception as e:
            _log.warning("failed to remove stale container %s: %s", name, e)
