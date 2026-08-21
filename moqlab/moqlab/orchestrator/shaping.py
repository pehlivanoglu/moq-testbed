"""Pure tc command synthesis for per-direction link shaping.

Replaces Mininet's TCLink/TCIntf machinery: instead of monkey-patching the
commands TCIntf synthesizes, moqlab builds the full egress qdisc chain itself
and runs it inside the owning container. The container's image provides the
tc binary, which is what makes dualpi2 usable at all — the router image ships
an iproute2 new enough for it, while the host and endpoint images do not.

Fixed handle scheme so chains are recognizable in `tc -s qdisc show`:

    5:  htb root (rate limiting), single class 5:1
    10: netem (delay / jitter / loss)
    20: AQM leaf (e.g. dualpi2)

Chain composition by spec contents:

    netem only              root netem
    rate only               htb
    rate + netem            htb -> netem
    rate + aqm              htb -> aqm
    rate + netem + aqm      htb -> netem -> aqm   (netem's single child slot)
    aqm only                root aqm
    netem + aqm             netem -> aqm

The root qdisc is installed with `tc qdisc replace`, which succeeds whether
the interface still has its default qdisc or a leftover chain — no
delete-the-old-root guard needed (the previous backend needed one because
TCIntf issued an unconditional `qdisc del root`).
"""

from __future__ import annotations

from moqlab.config.schema import AqmKind, DirectionSpec

# netem's default queue is 1000 packets; at high bandwidth-delay products it
# overflows and drops before the AQM ever sees pressure, silently becoming
# the real bottleneck. Raise it far past any queue we want to emulate.
_NETEM_LIMIT_PKTS = 50000

# An explicit quantum stops htb deriving one from r2q, which logs kernel
# warnings at the rates moqlab uses (the old backend monkey-patched r2q for
# the same reason).
_HTB_QUANTUM_BYTES = 1500


def _netem_args(spec: DirectionSpec) -> str:
    parts: list[str] = []
    if spec.delay_ms is not None:
        delay = f"delay {spec.delay_ms:g}ms"
        if spec.jitter_ms is not None:
            delay += f" {spec.jitter_ms:g}ms"
        parts.append(delay)
    if spec.loss_pct is not None:
        parts.append(f"loss {spec.loss_pct:g}%")
    parts.append(f"limit {_NETEM_LIMIT_PKTS}")
    return " ".join(parts)


def shaping_commands(
    iface: str, spec: DirectionSpec, aqm: AqmKind | None = None
) -> list[str]:
    """Ordered tc commands building the egress qdisc chain for one direction.

    Run them inside the node that owns `iface`. An all-None spec yields [].
    """
    has_rate = spec.bandwidth_mbps is not None
    has_netem = spec.delay_ms is not None or spec.loss_pct is not None
    has_aqm = aqm is not None

    cmds: list[str] = []
    parent: str | None = None  # None → the next qdisc becomes the root

    if has_rate:
        rate = f"{spec.bandwidth_mbps:g}mbit"
        cmds.append(f"tc qdisc replace dev {iface} root handle 5: htb default 1")
        cmds.append(
            f"tc class add dev {iface} parent 5: classid 5:1 htb "
            f"rate {rate} ceil {rate} burst 15k quantum {_HTB_QUANTUM_BYTES}"
        )
        parent = "5:1"

    if has_netem:
        netem = f"netem {_netem_args(spec)}"
        if parent is None:
            cmds.append(f"tc qdisc replace dev {iface} root handle 10: {netem}")
        else:
            cmds.append(f"tc qdisc add dev {iface} parent {parent} handle 10: {netem}")
        parent = "10:1"

    if has_aqm:
        aqm_name = aqm.value  # type: ignore[union-attr]
        if parent is None:
            cmds.append(f"tc qdisc replace dev {iface} root handle 20: {aqm_name}")
        else:
            cmds.append(f"tc qdisc add dev {iface} parent {parent} handle 20: {aqm_name}")

    return cmds


def offload_disable_commands(iface: str) -> list[str]:
    """Disable segmentation/receive offloads that distort shaping.

    With GSO/TSO/GRO on, the qdiscs see up-to-64KB superpackets instead of
    wire-sized ones, so rate limits, netem loss, and AQM marking all operate
    on the wrong units.
    """
    return [f"ethtool -K {iface} gso off tso off gro off"]
