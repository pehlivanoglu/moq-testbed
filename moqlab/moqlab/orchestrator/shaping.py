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

Initial setup installs the root with `tc qdisc replace`. Live value edits do
not reuse these construction commands: replacing an existing HTB root can
fail and rebuilding any root would flush queued packets. They instead change
the existing HTB class and netem qdisc in place.
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


def live_shaping_commands(
    iface: str, previous: DirectionSpec, updated: DirectionSpec
) -> list[str]:
    """Change shaping values without replacing qdiscs or flushing queues.

    Adding/removing HTB or netem changes the qdisc tree. That cannot preserve
    queued packets, so callers must reject such structural edits. Configure a
    bandwidth and a zero-valued netem field in YAML when those values need to
    remain editable throughout an experiment.
    """
    previous_has_rate = previous.bandwidth_mbps is not None
    updated_has_rate = updated.bandwidth_mbps is not None
    previous_has_netem = (
        previous.delay_ms is not None or previous.loss_pct is not None
    )
    updated_has_netem = updated.delay_ms is not None or updated.loss_pct is not None

    if (previous_has_rate, previous_has_netem) != (
        updated_has_rate,
        updated_has_netem,
    ):
        raise ValueError(
            "live edit would change the qdisc structure and flush queued packets; "
            "preconfigure bandwidth_mbps and loss_pct: 0 in YAML"
        )

    cmds: list[str] = []
    if previous.bandwidth_mbps != updated.bandwidth_mbps:
        rate = f"{updated.bandwidth_mbps:g}mbit"
        cmds.append(
            f"tc class change dev {iface} parent 5: classid 5:1 htb "
            f"rate {rate} ceil {rate} burst 15k quantum {_HTB_QUANTUM_BYTES}"
        )

    previous_netem = (
        previous.delay_ms,
        previous.jitter_ms,
        previous.loss_pct,
    )
    updated_netem = (
        updated.delay_ms,
        updated.jitter_ms,
        updated.loss_pct,
    )
    if previous_netem != updated_netem:
        parent = "parent 5:1" if updated_has_rate else "root"
        cmds.append(
            f"tc qdisc change dev {iface} {parent} handle 10: "
            f"netem {_netem_args(updated)}"
        )

    return cmds


def offload_disable_commands(iface: str) -> list[str]:
    """Disable segmentation/receive offloads that distort shaping.

    With GSO/TSO/GRO on, the qdiscs see up-to-64KB superpackets instead of
    wire-sized ones, so rate limits, netem loss, and AQM marking all operate
    on the wrong units.
    """
    return [f"ethtool -K {iface} gso off tso off gro off"]
