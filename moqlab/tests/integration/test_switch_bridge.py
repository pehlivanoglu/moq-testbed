"""Exercise real backend bridge/routing/qdisc commands without host sudo.

Run with MOQLAB_INTEGRATION=1; requires Docker, moqlab-router, sch_dualpi2.
Temporary privileged helper moves veths between isolated container namespaces.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import uuid
import time

import pytest

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.certs import generate_run_tls
from moqlab.config.synth import synthesize_relay_configs
from moqlab.orchestrator.containernet_backend import ContainernetBackend, ContainernetRunRecord
from moqlab.runtime import node_loopback_ips

pytestmark = pytest.mark.skipif(
    os.environ.get("MOQLAB_INTEGRATION") != "1", reason="set MOQLAB_INTEGRATION=1"
)


@pytest.mark.parametrize("native", [False, True], ids=["icmp", "native-quic"])
def test_real_bridge_shared_dualpi2(tmp_path: Path, native: bool):
    docker = pytest.importorskip("docker")
    client = docker.from_env()
    prefix = f"moqlab-switch-test-{uuid.uuid4().hex[:8]}"
    containers = []

    def run_container(name, **kwargs):
        image = kwargs.pop("image", "moqlab-router")
        container = client.containers.run(
            image, ["sleep", "300"], name=f"{prefix}-{name}",
            entrypoint=[], detach=True, network_mode="none", **kwargs,
        )
        containers.append(container)
        container.reload()
        return container

    def execute(container, command):
        result = container.exec_run(command)
        output = result.output.decode()
        assert result.exit_code == 0, f"{command}: {output}"
        return output

    class Node:
        def __init__(self, container):
            self.container = container

        def cmd(self, command):
            if "__MOQLAB_READY__" in command:
                return self.container.exec_run(["sh", "-c", command]).output.decode()
            return execute(self.container, ["sh", "-c", command])

    class Net:
        def __init__(self, helper):
            self.helper = helper
            self.nodes = {}

        def addDocker(self, name, **kwargs):
            node = Node(run_container(
                name, privileged=True, sysctls=kwargs.get("sysctls", {}),
                image=kwargs["dimage"] if native else "moqlab-router",
                volumes=kwargs.get("volumes", []),
                environment=kwargs.get("environment", {}),
            ))
            self.nodes[name] = node
            return node

        def addLink(self, a, b, intfName1, intfName2, params1, params2):
            execute(self.helper, [
                "ip", "link", "add", intfName1,
                "netns", str(a.container.attrs["State"]["Pid"]),
                "type", "veth", "peer", "name", intfName2,
                "netns", str(b.container.attrs["State"]["Pid"]),
            ])

        def get(self, name):
            return self.nodes[name]

    topology = TopologyConfig.model_validate({
        "relays": {
            name: {"listen_port": 9668 + i * 2, "admin_port": 9669 + i * 2}
            for i, name in enumerate(("origin", "c1", "c2"))
        },
        "routers": {"router": {"aqm": "dualpi2"}},
        "switches": {"sw": {}},
        "links": [
            {"from": "origin", "to": "router"},
            {"from": "router", "to": "sw", "forward": {"bandwidth_mbps": 4}},
            {"from": "sw", "to": "c1"},
            {"from": "sw", "to": "c2"},
        ],
    })
    if native:
        topology = load_topology(Path(__file__).resolve().parents[2] / "configs/examples/ex.yaml")
        assert all(topology.subscriber_media_client(sid) == "native" for sid in topology.subscribers)
        generate_run_tls(topology, tmp_path)
        configs = synthesize_relay_configs(topology, tmp_path / "configs")
    else:
        configs = {name: tmp_path / f"{name}.yaml" for name in topology.relays}
    try:
        helper = run_container("helper", privileged=True, pid_mode="host")
        net = Net(helper)
        record = ContainernetRunRecord(
            run_id=prefix, run_dir=tmp_path, loopback_ips=node_loopback_ips(topology),
        )
        ContainernetBackend()._build(
            net, topology, configs,
            record, lambda _: None,
        )
        for name, entries in record.node_iface_ips.items():
            for iface, address in entries:
                net.get(name).cmd(f"ip link set {iface} up")
                if address:
                    net.get(name).cmd(f"ip addr replace {address} dev {iface}")
        ContainernetBackend._configure_network(net, topology, record, lambda _: None)
        if native:
            for name in record.loopback_ips:
                for peer, address in record.loopback_ips.items():
                    if peer != name:
                        net.get(name).cmd(f"printf '{address} {peer}\\n' >> /etc/hosts")
            ContainernetBackend._launch_node_binaries(net, topology, record, print)

            def metrics(phase):
                response = execute(net.get("relay").container, ["bash", "-c",
                    'exec 3<>/dev/tcp/127.0.0.1/9669; '
                    'printf "GET /network-metrics HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n" >&3; cat <&3'
                ])
                rows = json.loads(response.split("\r\n\r\n", 1)[1])["clients"]
                selected = {row["connection_id"]: row for row in rows if row["subscribing"]}
                (tmp_path / f"{phase}.json").write_text(json.dumps(selected, indent=2))
                print(f"{phase}: {tmp_path / (phase + '.json')}", flush=True)
                return selected

            def fresh(rows):
                assert len(rows) == len(topology.subscribers), rows
                assert all(r["active"] and r["sample_age_ms"] < 2000 for r in rows.values()), rows

            baseline = metrics("baseline")
            def shared_bytes():
                qdiscs = json.loads(net.get("router").cmd("tc -j -s qdisc show dev router-eth1"))
                return next(q["bytes"] for q in qdiscs if q["kind"] == "dualpi2")

            before_bytes, before_time = shared_bytes(), time.monotonic()
            time.sleep(12)
            steady = metrics("steady")
            fresh(steady)
            assert steady.keys() == baseline.keys()
            assert all(r["ect1"] > baseline[k]["ect1"] for k, r in steady.items()), steady
            # Media is bursty: size from measured wire throughput, not one
            # instantaneous ACK-rate sample. Leave average-rate headroom;
            # frame bursts still exercise the shared queue's CE threshold.
            average_mbps = (shared_bytes() - before_bytes) * 8 / (time.monotonic() - before_time) / 1e6
            rate = max(0.5, average_mbps * 1.25)
            print(f"Shared capacity {rate:.3f} Mbps; baseline {average_mbps:.3f} Mbps", flush=True)
            net.get("router").cmd(
                f"tc class change dev router-eth1 parent 5: classid 5:1 htb rate {rate}mbit ceil {rate}mbit burst 15k quantum 1500"
            )
            time.sleep(15)
            congested = metrics("congested")
            fresh(congested)
            assert congested.keys() == steady.keys()
            assert all(r["ce"] > steady[k]["ce"] and r["window"]["ce"] > 0
                       for k, r in congested.items()), congested
            net.get("router").cmd(
                "tc class change dev router-eth1 parent 5: classid 5:1 htb rate 10000mbit ceil 10000mbit burst 15k quantum 1500"
            )
            time.sleep(15)
            recovered = metrics("recovered")
            fresh(recovered)
            assert recovered.keys() == steady.keys()
            assert all(r["ect1"] > congested[k]["ect1"] and r["window"]["ce"] == 0
                       and r["window"]["lost_packets"] == 0 for k, r in recovered.items()), recovered
            (tmp_path / "report.json").write_text(json.dumps({
                "shared_rate_mbps": rate, "baseline": steady,
                "congested": congested, "recovered": recovered,
            }, indent=2))
            print(f"Native QUIC marking and recovery passed: {tmp_path}", flush=True)
            return
        for name in ("c1", "c2"):
            net.get(name).cmd(f"ping -c 2 -W 2 {record.loopback_ips['origin']}")
            route = json.loads(net.get("router").cmd(
                f"ip -j route get {record.loopback_ips[name]}"
            ))
            assert route[0]["dev"] == "router-eth1"
        ports = json.loads(net.get("sw").cmd("ip -j addr show master br0"))
        assert len(ports) == 3
        assert all(not any(a["family"] == "inet" for a in p["addr_info"]) for p in ports)

        # ECT(1) packets for both destinations compete at the common bottleneck.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(
                net.get("origin").cmd,
                f"ping -Q 1 -i 0.001 -s 1200 -w 5 {record.loopback_ips[name]}",
            ) for name in ("c1", "c2")]
            for future in futures:
                future.result()
        stats = json.loads(net.get("router").cmd("tc -j -s qdisc show dev router-eth1"))
        dualpi2 = next(q for q in stats if q["kind"] == "dualpi2")
        assert dualpi2["packets"] > 0
        assert dualpi2["ecn-mark"] > 0, json.dumps(dualpi2)
    finally:
        if native:
            for name, node in getattr(locals().get("net"), "nodes", {}).items():
                log = node.container.exec_run(["sh", "-c", f"tail -n 100 /tmp/{name}.log 2>/dev/null"])
                if log.output:
                    (tmp_path / f"{name}.log").write_bytes(log.output)
        for container in reversed(containers):
            container.remove(force=True)
        client.close()
