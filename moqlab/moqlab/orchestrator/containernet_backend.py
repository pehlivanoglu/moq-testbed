"""Containernet backend.

Runs the same `TopologyConfig` as the Docker backend, but as Mininet hosts
inside Containernet. Wiring comes from `links:`: every link becomes one
direct host↔host veth pair (no switches), routers are first-class Docker
hosts with IP forwarding enabled, and per-direction shaping is applied with
explicit tc commands run inside the owning container (see
orchestrator/shaping.py for the qdisc chains).

Addressing: each link gets its own /24 from `_LINK_SUBNET_POOL` (.1 = from
side, .2 = to side), and every node additionally gets a canonical /32 on lo
from the 10.99.0.0/24 pool. /etc/hosts maps node names to the /32s and the
backend installs static /32 routes (BFS over the link graph), so application
traffic between any two nodes follows the declared physical path no matter
how many routers sit in between.

After everything is up, drops into the Containernet `CLI(net)` shell.
Exiting the shell tears the topology down.

This module imports mininet/containernet lazily so the rest of the package
remains usable on a developer laptop that doesn't have Containernet installed
(the Docker backend has no such dep). Import-time failure surfaces only when
the user actually runs `moqlab run --backend containernet`.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.config.synth import (
    synthesize_publisher_command,
    synthesize_relay_configs,
    synthesize_subscriber_command,
)
from moqlab.exceptions import OrchestratorError
from moqlab.orchestrator.routing import next_hops, route_commands
from moqlab.orchestrator.shaping import offload_disable_commands, shaping_commands
from moqlab.runtime import (
    all_node_ids,
    containernet_edge_interfaces,
    default_run_id,
    default_runs_dir,
    node_loopback_ips,
    relay_order,
    validate_run_id,
)

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

# We carve one /24 out of this /16 per topology link. Avoid Containernet's
# default 10.0.0.0/8 pool so our explicit subnets don't collide with anything
# Containernet auto-assigns.
_LINK_SUBNET_POOL = "10.20.0.0/16"

# Routers forward and must not interfere with the emulated paths: no reverse
# path filtering (asymmetric routes are legitimate experiment setups) and no
# ICMP redirects (redirects would teach endpoints to bypass the routed path).
# Containernet's addDocker forwards `sysctls` to Docker's host config, so
# these are in place before any process runs in the container.
_ROUTER_SYSCTLS = {
    "net.ipv4.ip_forward": "1",
    "net.ipv4.conf.all.rp_filter": "0",
    "net.ipv4.conf.default.rp_filter": "0",
    "net.ipv4.conf.all.send_redirects": "0",
    "net.ipv4.conf.default.send_redirects": "0",
}
_ENDPOINT_SYSCTLS = {
    "net.ipv4.conf.all.accept_redirects": "0",
    "net.ipv4.conf.default.accept_redirects": "0",
}


@dataclass
class ContainernetRunRecord:
    run_id: str
    run_dir: Path
    relays: list[str] = field(default_factory=list)
    routers: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    subscribers: list[str] = field(default_factory=list)
    # node id → canonical /32 loopback address (no prefix). Written to every
    # node's /etc/hosts and targeted by the static routes.
    loopback_ips: dict[str, str] = field(default_factory=dict)
    # (from, to) per link → (from-side IP, to-side IP) on that link's /24.
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

    return _LenientCLI, info, setLogLevel, Containernet


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
        if not topology.links:
            raise OrchestratorError(
                "the containernet backend requires explicit `links:` wiring; "
                "edges are no longer derived from upstream/connects_to "
                "(see moqlab/README.md)"
            )
        run_id = run_id or default_run_id()
        validate_run_id(run_id)

        CLI, info, setLogLevel, Containernet = _import_mininet()
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
        _modprobe_aqm_modules(topology)

        # No switches and therefore no controller: every link is a direct
        # host↔host veth pair and routers do the forwarding.
        net = Containernet(controller=None)
        record = ContainernetRunRecord(
            run_id=run_id,
            run_dir=run_dir,
            loopback_ips=node_loopback_ips(topology),
        )

        try:
            self._build(net, topology, relay_yaml_paths, record, info)

            # /etc/hosts must be in place before we launch node binaries.
            # The containers are already running at this point (addDocker
            # called dcli.start()), but Mininet's host.cmd() machinery is not
            # ready until net.start(), so direct docker exec is the earliest
            # reliable way to append the name → /32 entries.
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

            # Routes and shaping must exist before any moqx/pub/sub process
            # resolves and dials its peer.
            self._configure_network(net, topology, record, info)
            self._sanity_ping(net, topology, record, info)
            self._launch_node_binaries(net, topology, record, info)

            info(f"\n*** Containernet run_id={run_id}\n")
            info("***   Try: <node> ping <other_node>\n")
            info("***   Try: <node> tc -s qdisc show\n")
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
    ) -> None:
        """Add every node and link to `net`. Pure construction; no start()."""
        nodes: dict[str, object] = {}
        info(f"*** moqlab containernet run_id={record.run_id}\n")

        edges = containernet_edge_interfaces(topology)
        link_subnets = list(
            ipaddress.ip_network(_LINK_SUBNET_POOL).subnets(new_prefix=24)
        )
        if len(edges) > len(link_subnets):
            raise OrchestratorError(
                f"topology has {len(edges)} links but link subnet pool "
                f"{_LINK_SUBNET_POOL} only provides {len(link_subnets)} /24 subnets"
            )

        for rid in relay_order(topology):
            cfg = relay_yaml_paths[rid].resolve()
            nodes[rid] = net.addDocker(
                rid,
                dimage=topology.relay_image(rid),
                volumes=[f"{cfg}:/etc/moqx/relay.yaml:ro"],
                sysctls=dict(_ENDPOINT_SYSCTLS),
            )
            record.relays.append(rid)

        for rid in topology.routers:
            nodes[rid] = net.addDocker(
                rid,
                dimage=topology.router_image(rid),
                sysctls=dict(_ROUTER_SYSCTLS),
            )
            record.routers.append(rid)

        # No `dcmd` for publishers/subscribers: Containernet wipes the image
        # ENTRYPOINT at container create time, so passing argv flags here
        # makes Docker try to exec the first flag as a binary. Instead we
        # spawn the binary explicitly in `_launch_node_binaries` after
        # net.start(). Routers intentionally run nothing.
        for pid in topology.publishers:
            nodes[pid] = net.addDocker(
                pid,
                dimage=topology.publisher_image(pid),
                sysctls=dict(_ENDPOINT_SYSCTLS),
            )
            record.publishers.append(pid)

        for sid in topology.subscribers:
            nodes[sid] = net.addDocker(
                sid,
                dimage=topology.subscriber_image(sid),
                sysctls=dict(_ENDPOINT_SYSCTLS),
            )
            record.subscribers.append(sid)

        # One /24 per link, direct host↔host veth pair, both sides addressed
        # explicitly (multi-link nodes need one IP per interface, so we never
        # rely on Containernet's auto-assignment).
        subnet_iter = iter(link_subnets)
        for edge in edges:
            sub = next(subnet_iter)
            a_ip = f"{sub.network_address + 1}/{sub.prefixlen}"
            b_ip = f"{sub.network_address + 2}/{sub.prefixlen}"

            net.addLink(
                nodes[edge.a],
                nodes[edge.b],
                params1={"ip": a_ip},
                params2={"ip": b_ip},
            )

            record.edge_ips[(edge.a, edge.b)] = (
                str(sub.network_address + 1),
                str(sub.network_address + 2),
            )
            record.node_iface_ips.setdefault(edge.a, []).append((edge.a_iface, a_ip))
            record.node_iface_ips.setdefault(edge.b, []).append((edge.b_iface, b_ip))

    @staticmethod
    def _configure_network(
        net, topology: TopologyConfig, record: ContainernetRunRecord, info
    ) -> None:
        """Loopbacks, forwarding, offloads, static routes, shaping — in that order.

        Runs after net.start() (host.cmd needs it) and strictly before
        `_launch_node_binaries`, because moqx resolves and dials its upstream
        at startup.
        """
        node_ids = all_node_ids(topology)

        info("*** Assigning canonical /32 loopback addresses\n")
        for nid in node_ids:
            node = net.get(nid)
            node.cmd("ip link set lo up")
            node.cmd(f"ip addr replace {record.loopback_ips[nid]}/32 dev lo")

        # Belt and braces for Containernet builds without sysctl passthrough.
        # The sysctl(8) binary is not in the images; write /proc/sys directly.
        for rid in record.routers:
            net.get(rid).cmd("echo 1 > /proc/sys/net/ipv4/ip_forward")

        info("*** Disabling NIC offloads on link interfaces\n")
        for nid, iface_ips in record.node_iface_ips.items():
            node = net.get(nid)
            for iface, _ in iface_ips:
                for cmd in offload_disable_commands(iface):
                    node.cmd(cmd)

        info("*** Installing static /32 routes\n")
        edges = containernet_edge_interfaces(topology)
        neighbor_addrs: dict[str, dict[str, tuple[str, str]]] = {}
        for edge in edges:
            a_ip, b_ip = record.edge_ips[(edge.a, edge.b)]
            neighbor_addrs.setdefault(edge.a, {})[edge.b] = (b_ip, edge.a_iface)
            neighbor_addrs.setdefault(edge.b, {})[edge.a] = (a_ip, edge.b_iface)
        hops = next_hops(node_ids, [(edge.a, edge.b) for edge in edges])
        for nid in node_ids:
            node = net.get(nid)
            for cmd in route_commands(
                nid, hops[nid], record.loopback_ips, neighbor_addrs.get(nid, {})
            ):
                node.cmd(cmd)

        info("*** Applying per-direction link shaping\n")
        for edge, link in zip(edges, topology.links):
            for nid, iface, spec in (
                (edge.a, edge.a_iface, link.forward),
                (edge.b, edge.b_iface, link.reverse),
            ):
                node = net.get(nid)
                for cmd in shaping_commands(iface, spec):
                    out = node.cmd(cmd)
                    if out and out.strip():
                        _log.warning("%s: %r printed: %s", nid, cmd, out.strip())

    @staticmethod
    def _sanity_ping(
        net, topology: TopologyConfig, record: ContainernetRunRecord, info
    ) -> None:
        """Best-effort ping along every application edge; warn-only."""
        app_edges = [
            *((rid, r.upstream) for rid, r in topology.relays.items() if r.upstream),
            *((pid, p.connects_to) for pid, p in topology.publishers.items()),
            *((sid, s.connects_to) for sid, s in topology.subscribers.items()),
        ]
        info("*** Sanity ping over routed paths\n")
        for src, dst in app_edges:
            out = net.get(src).cmd(f"ping -c1 -W2 {record.loopback_ips[dst]}")
            if "not found" in out:
                _log.warning("%s has no ping binary; skipping sanity pings", src)
                return
            if " 0% packet loss" not in out:
                _log.warning("sanity ping %s -> %s failed:\n%s", src, dst, out)

    @staticmethod
    def _launch_node_binaries(
        net, topology: TopologyConfig, record: ContainernetRunRecord, info
    ) -> None:
        """Start moqx / moqdateserver / moqtextclient inside each container.

        Containernet wipes the image ENTRYPOINT at container create time AND
        does NOT invoke Docker.start() during net.start() (despite the
        misleading comment in containernet/mininet/node.py:Docker.start),
        so nothing in the image is auto-launched. We launch every node's
        binary explicitly here, after `net.start()` and the network
        configuration. Routers run no binary at all.

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


def _shell_quote_argv(argv: list[str]) -> list[str]:
    """Shell-quote each argv element so dcmd survives shell interpretation."""
    import shlex

    return [shlex.quote(a) for a in argv]


def _sleep_if_configured(seconds: float, reason: str, info) -> None:
    if seconds <= 0:
        return
    info(f"*** waiting {seconds:g}s {reason}\n")
    time.sleep(seconds)


def _modprobe_aqm_modules(topology: TopologyConfig) -> None:
    """Best-effort host-side modprobe for AQM qdiscs the topology uses.

    tc inside a container netns normally triggers kernel module autoload, but
    be explicit: we already run as root on the host, and a missing module
    should surface before shaping commands run.
    """
    kinds = {
        direction.aqm.value
        for link in topology.links
        for direction in (link.forward, link.reverse)
        if direction.aqm is not None
    }
    for kind in sorted(kinds):
        module = f"sch_{kind}"
        try:
            subprocess.run(["modprobe", module], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            _log.warning(
                "modprobe %s failed (%s); relying on kernel autoload", module, e
            )


def _write_etc_hosts_via_docker(record: ContainernetRunRecord, info) -> None:
    """Append `<loopback /32> <name>` for every other node, via docker exec.

    Runs before net.start() so all nodes can resolve every peer before
    `_launch_node_binaries()` starts moqx / pub / sub. We can't use Mininet's
    `host.cmd()` yet — its shell-in-ns machinery only comes up at net.start().
    Containers themselves are running (Containernet's addDocker creates and
    starts them), so direct docker exec works.

    The node's own entry is skipped: Docker already wrote a line resolving the
    container's hostname, and glibc takes the first match in /etc/hosts.
    """
    try:
        import docker
    except ImportError as e:
        raise OrchestratorError(
            "failed to populate /etc/hosts: Python Docker SDK is not installed"
        ) from e
    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:
        raise OrchestratorError(
            f"failed to populate /etc/hosts: Docker daemon is unreachable ({e})"
        ) from e

    for nid in sorted(record.loopback_ips):
        entries = [
            f"{ip} {other}"
            for other, ip in sorted(record.loopback_ips.items())
            if other != nid
        ]
        if not entries:
            continue
        try:
            container = client.containers.get(f"mn.{nid}")
        except docker.errors.NotFound as e:
            raise OrchestratorError(
                f"failed to populate /etc/hosts: container mn.{nid} not found"
            ) from e
        # `>>` rather than `>` to keep Docker's own /etc/hosts entries. Node
        # ids and IPs are validated character sets, so single quotes are safe.
        payload = "".join(f"{entry}\\n" for entry in entries)
        result = container.exec_run(["sh", "-c", f"printf '{payload}' >> /etc/hosts"])
        exit_code = getattr(result, "exit_code", 0)
        if exit_code != 0:
            output = getattr(result, "output", b"")
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            raise OrchestratorError(
                f"failed to populate /etc/hosts in mn.{nid}: "
                f"docker exec exited {exit_code}: {output}"
            )


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

    expected = [f"mn.{nid}" for nid in all_node_ids(topology)]
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
