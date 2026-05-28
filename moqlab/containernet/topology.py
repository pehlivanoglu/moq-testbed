#!/usr/bin/env python3
"""
    pub (10.0.0.1) ──[TCLink]── s1 ── relay-eth0 (10.0.0.2)
                                                       │
                                                  relay-eth1 (10.0.1.2)
                                                       │
                                                       s2 ──[TCLink]── sub (10.0.1.3)

"""

from pathlib import Path

from mininet.net import Containernet
from mininet.node import OVSBridge
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import info, setLogLevel


REPO_ROOT = Path(__file__).resolve().parents[2]
RELAY_YAML = REPO_ROOT / "relay-a.yaml"

PUB_IP = "10.0.0.1/24"
RELAY_PUB_IP = "10.0.0.2/24"   # relay-eth0, faces pub via s1
RELAY_SUB_IP = "10.0.1.2/24"   # relay-eth1, faces sub via s2
SUB_IP = "10.0.1.3/24"

RELAY_PORT = 9668
RELAY_ENDPOINT = "/moq-relay"
NAMESPACE = "moq-date"
TRACK_NAME = "date"

# Per-leg shaping. Tweak independently to model asymmetric access networks.
PUB_LEG = {"delay": "31ms",  "bw": 100}   # publisher uplink
SUB_LEG = {"delay": "69ms", "bw": 20}    # subscriber, slower link


def _ip(s):
    return s.split("/", 1)[0]


def run():
    if not RELAY_YAML.is_file():
        raise SystemExit(f"relay yaml not found at {RELAY_YAML}")

    net = Containernet(switch=OVSBridge, link=TCLink)

    info("*** Adding switches (standalone OVS bridges — MAC-learning, no controller)\n")
    s1 = net.addSwitch("s1", failMode="standalone")
    s2 = net.addSwitch("s2", failMode="standalone")

    info("*** Adding Docker hosts\n")
    relay = net.addDocker(
        "relay",
        ip=RELAY_PUB_IP,
        dimage="moqlab-relay",
        volumes=[f"{RELAY_YAML}:/etc/moqx/relay.yaml:ro"],
    )
    pub = net.addDocker("pub", ip=PUB_IP, dimage="moqlab-pub")
    sub = net.addDocker("sub", ip=SUB_IP, dimage="moqlab-sub")

    info("*** Adding links (TCLink on each host↔switch leg for shaping)\n")
    net.addLink(pub, s1, cls=TCLink, **PUB_LEG)
    net.addLink(relay, s1, cls=TCLink, **PUB_LEG)
    net.addLink(relay, s2, cls=TCLink, params1={"ip": RELAY_SUB_IP}, **SUB_LEG)
    net.addLink(sub, s2, cls=TCLink, **SUB_LEG)

    info("*** Starting network\n")
    net.start()

    # mininet brings switch ports up via the switch's start(); host-side
    # veth ends still need an explicit kick or they sit in state DOWN.
    info("*** Bringing host-side interfaces up\n")
    for host, iface, ip_cidr in (
        (pub,   "pub-eth0",   PUB_IP),
        (relay, "relay-eth0", RELAY_PUB_IP),
        (relay, "relay-eth1", RELAY_SUB_IP),
        (sub,   "sub-eth0",   SUB_IP),
    ):
        host.cmd(f"ip link set {iface} up")
        host.cmd(f"ip addr replace {ip_cidr} dev {iface}")

    info("*** Ping checks\n")
    net.ping([pub, relay])
    net.ping([sub, relay])

    info("*** Starting relay\n")
    relay.cmd(
        "/usr/local/bin/moqx --config /etc/moqx/relay.yaml "
        "> /tmp/relay.log 2>&1 &"
    )
    relay.cmd("sleep 2")

    info("*** Starting publisher (moqdateserver)\n")
    pub.cmd(
        "/usr/local/bin/moqdateserver "
        f"--ns={NAMESPACE} "
        f"--relay_url=https://{_ip(RELAY_PUB_IP)}:{RELAY_PORT}{RELAY_ENDPOINT} "
        "--insecure --logging=INFO "
        "> /tmp/pub.log 2>&1 &"
    )
    pub.cmd("sleep 3")

    info("*** Starting subscriber (moqtextclient)\n")
    sub.cmd(
        "/usr/local/bin/moqtextclient "
        f"--connect_url=https://{_ip(RELAY_SUB_IP)}:{RELAY_PORT}{RELAY_ENDPOINT} "
        f"--track_namespace={NAMESPACE} "
        f"--track_name={TRACK_NAME} "
        "--insecure --logging=INFO "
        "> /tmp/sub.log 2>&1 &"
    )

    info("\n*** Entering Containernet CLI\n")
    info("***   sub   tail -f /tmp/sub.log      # date messages arriving\n")
    info("***   relay tail -f /tmp/relay.log\n")
    info("***   pub   tail -f /tmp/pub.log\n")
    info("***   pub   ping -c2 10.0.0.2        # ping relay via s1\n")
    info("***   sub   ping -c2 10.0.1.2        # ping relay via s2\n")
    info("***   sh    ovs-vsctl show           # inspect bridges\n\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
