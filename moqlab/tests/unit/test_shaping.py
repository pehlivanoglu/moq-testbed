"""Unit tests for orchestrator/shaping.py qdisc chain synthesis."""

import pytest

from moqlab.config.schema import AqmKind, DirectionSpec
from moqlab.orchestrator.shaping import (
    live_shaping_commands,
    offload_disable_commands,
    shaping_commands,
)


def test_empty_spec_yields_no_commands():
    assert shaping_commands("r1-eth0", DirectionSpec()) == []


def test_netem_only_is_root_netem():
    spec = DirectionSpec(delay_ms=20)
    assert shaping_commands("r1-eth0", spec) == [
        "tc qdisc replace dev r1-eth0 root handle 10: netem delay 20ms limit 50000",
    ]


def test_netem_renders_jitter_and_loss():
    spec = DirectionSpec(delay_ms=20, jitter_ms=5, loss_pct=1)
    assert shaping_commands("r1-eth0", spec) == [
        "tc qdisc replace dev r1-eth0 root handle 10: "
        "netem delay 20ms 5ms loss 1% limit 50000",
    ]


def test_loss_only_is_valid_netem():
    spec = DirectionSpec(loss_pct=0.5)
    assert shaping_commands("r1-eth0", spec) == [
        "tc qdisc replace dev r1-eth0 root handle 10: netem loss 0.5% limit 50000",
    ]


def test_rate_only_is_htb_with_explicit_quantum():
    spec = DirectionSpec(bandwidth_mbps=50)
    assert shaping_commands("r1-eth0", spec) == [
        "tc qdisc replace dev r1-eth0 root handle 5: htb default 1",
        "tc class add dev r1-eth0 parent 5: classid 5:1 htb "
        "rate 50mbit ceil 50mbit burst 15k quantum 1500",
    ]


def test_rate_plus_netem_chains_netem_under_htb():
    spec = DirectionSpec(bandwidth_mbps=50, delay_ms=20)
    cmds = shaping_commands("r1-eth0", spec)
    assert cmds[:2] == shaping_commands("r1-eth0", DirectionSpec(bandwidth_mbps=50))
    assert cmds[2] == (
        "tc qdisc add dev r1-eth0 parent 5:1 handle 10: netem delay 20ms limit 50000"
    )


def test_rate_plus_aqm_chains_aqm_under_htb():
    spec = DirectionSpec(bandwidth_mbps=20)
    cmds = shaping_commands("r1-eth0", spec, AqmKind.dualpi2)
    assert cmds[2] == "tc qdisc add dev r1-eth0 parent 5:1 handle 20: dualpi2"


def test_full_chain_uses_netem_child_slot_for_aqm():
    spec = DirectionSpec(bandwidth_mbps=20, delay_ms=5)
    cmds = shaping_commands("r1-eth0", spec, AqmKind.dualpi2)
    assert cmds[2] == (
        "tc qdisc add dev r1-eth0 parent 5:1 handle 10: netem delay 5ms limit 50000"
    )
    assert cmds[3] == "tc qdisc add dev r1-eth0 parent 10:1 handle 20: dualpi2"


def test_aqm_only_is_root_aqm():
    spec = DirectionSpec()
    assert shaping_commands("r1-eth0", spec, AqmKind.dualpi2) == [
        "tc qdisc replace dev r1-eth0 root handle 20: dualpi2",
    ]


def test_netem_plus_aqm_without_rate():
    spec = DirectionSpec(delay_ms=10)
    assert shaping_commands("r1-eth0", spec, AqmKind.dualpi2) == [
        "tc qdisc replace dev r1-eth0 root handle 10: netem delay 10ms limit 50000",
        "tc qdisc add dev r1-eth0 parent 10:1 handle 20: dualpi2",
    ]


def test_root_qdisc_always_uses_replace():
    for spec in (
        DirectionSpec(delay_ms=1),
        DirectionSpec(bandwidth_mbps=1),
        DirectionSpec(),
    ):
        first = shaping_commands(
            "x-eth0", spec, AqmKind.dualpi2 if spec.is_noop() else None
        )[0]
        assert first.startswith("tc qdisc replace dev x-eth0 root ")


def test_jitter_without_delay_rejected_by_schema():
    with pytest.raises(ValueError, match="jitter_ms requires delay_ms"):
        DirectionSpec(jitter_ms=5)


def test_is_noop():
    assert DirectionSpec().is_noop()
    assert not DirectionSpec(delay_ms=0).is_noop()


def test_offload_disable_commands():
    assert offload_disable_commands("sub-eth0") == [
        "ethtool -K sub-eth0 gso off tso off gro off",
    ]


def test_live_changes_rate_and_netem_without_replacing_qdiscs():
    previous = DirectionSpec(
        bandwidth_mbps=20, delay_ms=0, jitter_ms=0, loss_pct=0
    )
    updated = DirectionSpec(
        bandwidth_mbps=10, delay_ms=25, jitter_ms=3, loss_pct=1
    )

    assert live_shaping_commands("r1-eth0", previous, updated) == [
        "tc class change dev r1-eth0 parent 5: classid 5:1 htb "
        "rate 10mbit ceil 10mbit burst 15k quantum 1500",
        "tc qdisc change dev r1-eth0 parent 5:1 handle 10: "
        "netem delay 25ms 3ms loss 1% limit 50000",
    ]


def test_live_root_netem_change_preserves_qdisc():
    previous = DirectionSpec(loss_pct=0)
    updated = DirectionSpec(delay_ms=10, jitter_ms=2, loss_pct=0.5)

    assert live_shaping_commands("r1-eth0", previous, updated) == [
        "tc qdisc change dev r1-eth0 root handle 10: "
        "netem delay 10ms 2ms loss 0.5% limit 50000",
    ]


@pytest.mark.parametrize(
    ("previous", "updated"),
    [
        (DirectionSpec(), DirectionSpec(bandwidth_mbps=10)),
        (DirectionSpec(bandwidth_mbps=10), DirectionSpec()),
        (
            DirectionSpec(bandwidth_mbps=10),
            DirectionSpec(bandwidth_mbps=10, loss_pct=1),
        ),
    ],
)
def test_live_structural_change_rejected(previous, updated):
    with pytest.raises(ValueError, match="flush queued packets"):
        live_shaping_commands("r1-eth0", previous, updated)
