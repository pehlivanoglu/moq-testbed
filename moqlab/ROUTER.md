# Router And Link Emulator Architecture

This note describes how moqlab models network links now that ECN, L4S, and
AQM behavior are first-class research targets. It used to be a future-
direction sketch; the design below is implemented.

## Model

The Containernet backend builds exactly what `links:` declares. Every link is
one direct host↔host veth pair. Optional `switches:` join ports into Linux
bridges for shared LANs. Routers are declared
in `routers:` and are ordinary Docker hosts (image `moqlab-router`) with IP
forwarding enabled; they run no MoQ binary. CDN relays, publishers, and
subscribers stay application containers.

```text
node-a ── rt-1 ── node-b
          └── [HTB rate → AQM] on bottleneck egress
```

The router owns the bottleneck queue. That separation is the point: relays
own application behavior (caching, fan-out, MoQ semantics), routers own
network behavior (queueing, marking, dropping, forwarding).

## Addressing and routing

- Each link gets a /24 from `10.20.0.0/16` (`.1` = `from` side, `.2` = `to`
  side).
- Each node gets a canonical /32 on `lo` from `10.99.0.0/24`, assigned in
  declaration order (relays, routers, publishers, subscribers, then optional
  traffic endpoints).
- `/etc/hosts` on every node maps every peer name to its /32, so the
  generated moqx/pub/sub URLs (`moqt://relay-a:9668/...`) are path-
  independent.
- The orchestrator computes BFS next hops over the link graph
  (`orchestrator/routing.py`, deterministic tie-break via sorted adjacency)
  and installs one `ip route replace <dst>/32 via <neighbor> dev <iface>
  src <own /32>` per destination on every node. No routing daemons.
- The topology schema still never asks the user for an IP address.
- External traffic named paths add generated sender aliases from
  `10.100.0.0/24` and receiver aliases from `10.101.0.0/24`. Explicit
  symmetric `/32` routes force each alias pair through its declared router
  sequence, even when several paths connect the same two traffic containers.

Router containers get `net.ipv4.ip_forward=1`, `rp_filter=0`, and
`send_redirects=0`; endpoints get `accept_redirects=0` so a forwarding hop
can never teach an endpoint to bypass the emulated path.

## Per-direction shaping

`links:` entries carry `forward:` (from→to) and `reverse:` (to→from) blocks
for rate, delay, jitter, and loss. Each router carries one optional `aqm`;
that AQM is appended to every egress chain owned by the router.
Each block compiles to an egress qdisc chain on the owning interface
(`orchestrator/shaping.py`), with fixed handles so `tc -s qdisc show` is
always readable: htb `5:`/class `5:1`, netem `10:`, AQM `20:`.

| Spec | Chain |
|---|---|
| netem fields only | root netem |
| `bandwidth_mbps` only | root htb (explicit `quantum`, no r2q warnings) |
| rate + netem | htb → netem |
| rate + `aqm` | htb → AQM |
| rate + netem + `aqm` | htb → netem → AQM (netem's single child slot) |
| `aqm` only | root AQM |
| netem + `aqm` | netem → AQM |

netem gets an explicit large `limit` so its default 1000-packet limit does not
cause unintended loss. This does not make a combined htb → netem → AQM chain
a faithful bottleneck: netem holds delayed packets before feeding its child,
so the AQM does not own the full backlog.

`defaults.link.forward` / `defaults.link.reverse` supply per-direction fields
that every link inherits, so each `links:` entry states only what differs.
Inheritance is per-field, not per-block: a link setting `bandwidth_mbps` keeps
an inherited `delay_ms`. Defaults are folded in during config validation, so
`link.forward` / `link.reverse` are already the effective specs everywhere
downstream.

Three states, not two — `null` is not `0`:

| Link writes | Effective value |
|---|---|
| field omitted | inherited from `defaults.link` |
| `delay_ms: 25` | 25 (overrides default; `0` is a real value, not "unset") |
| `delay_ms: null` | cleared — no netem at all |

`null` is what a bottleneck link uses to drop inherited delay and keep its
router-owned chain at htb → AQM.

Caveat: htb → netem → AQM is valid tc syntax but unsuitable for clean AQM
experiments. Put propagation netem on a separate, non-AQM egress and keep the
rate+AQM bottleneck at htb → AQM. The shipped shared-bottleneck example puts
zero-valued, live-editable netem on each switch↔subscriber direction.

`aqm` (currently `dualpi2`) is configured once on a router and applies to all
its egress interfaces. Optional `dualpi2_target_ms` overrides the kernel's
15 ms PI2 target; it must be positive and requires `aqm: dualpi2`. This is the
Classic PI2 target, not the L-queue's default 1 ms step threshold and not
mvfst's sender-side `l4s_ce_target`. This is also an iproute2-version constraint:
endpoint images ship distro iproute2, while `Dockerfile.router` builds a
pinned modern iproute2 whose tc knows dualpi2. The kernel side
(`sch_dualpi2`) comes from the host kernel; the backend runs `modprobe`
host-side when a topology uses an AQM, and `moqlab doctor` warns when the
module is missing.

GSO/TSO/GRO are disabled on every link interface — offloaded superpackets
would otherwise hit the qdiscs as 64KB units and distort rate limiting, loss,
and marking granularity.

## Runtime tweaks

Initial qdiscs come from the YAML. With `moqlab run --visualize`, select a
link to change rate/netem fields or select a router to change its AQM on all
egress interfaces. Value-only link edits use `tc class change` for HTB and
`tc qdisc change` for netem, preserving queued packets, qdisc statistics, and
AQM state. A live edit that would add or remove HTB/netem is rejected because
changing the qdisc hierarchy would flush queued packets. Preconfigure both a
bandwidth and a zero-valued netem field when they must remain editable, e.g.
`bandwidth_mbps: 100` plus `loss_pct: 0`; delay and jitter can then be changed
without rebuilding the hierarchy. Do this only on non-AQM links; keep netem
off a shared htb → DualPI2 bottleneck. Runtime changes do not rewrite YAML. Direct
`tc` inspection remains available, e.g.:

```bash
docker exec mn.rt-1 tc qdisc show dev rt-1-eth1
docker exec mn.rt-1 tc class change dev rt-1-eth1 parent 5: classid 5:1 htb rate 10mbit ceil 10mbit burst 15k quantum 1500
```

(Or run the same command from the Mininet CLI: `rt-1 tc ...`.)

## Routing scope

Two different meanings of routing in this testbed:

- **Application-level relay routing** (subscriber picks relay-b instead of
  relay-c): modeled in the relay graph / generated moqx config. Does not
  involve `routers:`.
- **Network-level IP routing** (relay-a → rt-1 → rt-2 → relay-b): modeled
  with `routers:` + `links:` as described above.

## ECN end-to-end

### Shared bottleneck with one router

`configs/examples/ex.yaml` uses `relay -> router -> switch -> subscribers`.
Declare `switches: { switch: {} }` and connect the router to the switch, then
the switch to each subscriber. Subscribers still `connects_to: relay`.
The switch runs an unmanaged Linux bridge (`br0`) inside the existing router
image; override its `image` if needed. It has no routed IP or router AQM.

The example's shared HTB -> DualPI2 chain is on `router-eth1`. Start
uncongested, then edit the router-to-switch forward bandwidth in the
visualizer, or run:

```bash
docker exec mn.router tc class change dev router-eth1 parent 5: classid 5:1 htb rate 4mbit ceil 4mbit burst 15k quantum 1500
docker exec mn.router tc -s qdisc show dev router-eth1
```

4 Mbps is aggregate capacity shared by all subscribers. Choose a rate below
aggregate offered traffic to induce marking; excessive restriction causes
loss. Restore `10000mbit` and check fresh per-client CE deltas decay while
traffic continues. Lifetime CE counts do not reset.

The router declares `dualpi2_target_ms: 15` explicitly. Change it in YAML to
test another PI2 target. The designer exposes this field; the live visualizer
does not yet change it in place.

Bridge ports stay unnumbered. Connected switches form one /24 LAN, with
unique addresses on attached IP nodes and unchanged canonical /32 identities.
Routes skip bridges as next hops. Switch chains are supported; Layer 2 loops
and duplicate node attachments to one LAN are rejected because STP is off.
Each LAN supports 254 IP ports. Explicit external-traffic paths still require
router intermediates. Switch topologies require Containernet.

### Bandwidth, latency and jitter placement

Set aggregate downstream bandwidth on `router -> switch` **forward**.
Keep that interface's chain HTB -> DualPI2. For individual client delay,
set netem on `switch -> subscriber` forward and/or reverse:

```yaml
- from: router
  to: switch
  forward: { bandwidth_mbps: 4 }
- from: switch
  to: sub-r
  forward: { delay_ms: 10, jitter_ms: 2 }
  reverse: { delay_ms: 10, jitter_ms: 2 }
```

Here forward delay runs on the switch output to `sub-r`; reverse delay runs
on `sub-r`'s output to the switch. This adds about 20 ms mean RTT, with jitter
in both directions. Repeat for other subscribers if their paths need delay.
Optional bandwidth on a subscriber link adds a separate per-client bottleneck;
omit it when testing only shared congestion.

For common propagation delay before fan-out, use relay -> router forward
(relay egress) and switch -> router (the reverse of router -> switch) instead.
This keeps netem off the downstream shared AQM output. Do not duplicate delay
on common and per-client links unless their sum is intended.

`ex.yaml` sets `delay_ms: 0`, `jitter_ms: 0`, and `loss_pct: 0` on both
directions of every switch↔subscriber link. Change those values live without
rebuilding qdiscs. `null` means no netem exists; changing the last null netem
field to zero or back would alter the hierarchy and is intentionally rejected.

Run the optional real bridge/routing/AQM check from `moqlab/`:

```bash
MOQLAB_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_switch_bridge.py
```

It requires Docker, the router/relay/native media images and host `sch_dualpi2`.
Temporary privileged containers execute actual backend commands. The `icmp`
case verifies shared-queue CE marking; `native-quic` starts `ex.yaml`'s native
media processes, checks fresh ECT(1) increments, CE feedback on every subscriber,
and recovery with unchanged connection IDs and zero recent CE/loss.
Per-phase JSON and process logs are retained in pytest's temporary directory.
Use `-k icmp` or `-k native` to run one case. This exercises backend wiring and
launch commands through a Docker adapter; it does not exercise Mininet's CLI.

Native test capacity uses aggregate wire throughput measured over 12 seconds,
with average-rate headroom so frame bursts induce CE. A single instantaneous
ACK-rate sample can underestimate offered traffic and cause severe overload.
Successful CE feedback alone does not establish healthy congestion control;
check freshness, loss, media progress and recovery too.

Validation on 2026-09-14: all 211 unit tests passed after restoring the two
missing example fixtures. Native QUIC test passed using three subscribers:
3.136 Mbps measured baseline, 3.920 Mbps shared capacity, and 985–1053 recent
CE marks per subscriber under frame bursts. The same connection IDs remained
fresh; 15 seconds after restoring capacity, recent CE and loss were zero and
ECT(1) continued increasing. Congestion included retransmissions; this result
does not establish loss-free operation. A stronger initial restriction caused
stale samples, so its run was rejected rather than counted as recovery evidence.

The testbed plumbing (DualPI2 marking CE at the bottleneck) is necessary but
not sufficient for L4S results: the QUIC transport must send ECT(1) and react
to CE. Set `l4s_ce_target` to a value in `(0, 1)` on a relay to enable mvfst
L4S ECN for connections accepted by that relay; omitting it leaves ECN
disabled. Current moqlab synthesis does not expose `quic.cc_algo`, so moqx
keeps its BBR default. In the pinned mvfst source, L4S weight/target congestion
response is implemented by Cubic, not BBR. Current tests therefore prove
ECT(1), validation, CE feedback, and recovery after capacity restoration—not
a CE-driven scalable congestion response. Expose compatible CC selection and
test CE-driven response before claiming full L4S behavior. Transport ECN
counters are exported at `/network-metrics`, with recent values in each
client's `window` object.
