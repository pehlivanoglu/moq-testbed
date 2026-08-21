# Router And Link Emulator Architecture

This note describes how moqlab models network links now that ECN, L4S, and
AQM behavior are first-class research targets. It used to be a future-
direction sketch; the design below is implemented.

## Model

The Containernet backend builds exactly what `links:` declares. Every link is
one direct host↔host veth pair — there are no switches. Routers are declared
in `routers:` and are ordinary Docker hosts (image `moqlab-router`) with IP
forwarding enabled; they run no MoQ binary. CDN relays, publishers, and
subscribers stay application containers.

```text
node-a ── rt-1 ── node-b
          └── [HTB rate → netem → AQM] on rt-1's egress
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

netem gets an explicit large `limit` so its default 1000-packet queue never
becomes the real bottleneck ahead of the AQM.

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

Caveat: in the htb → netem → AQM chain the delay sits upstream of the AQM on
the same interface. For the cleanest L4S experiments, put propagation delay
on the endpoint sides of links and the rate+AQM bottleneck on the router
egress — the shipped examples follow that pattern.

`aqm` (currently `dualpi2`) is configured once on a router and applies to all
its egress interfaces. This is also an iproute2-version constraint:
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
egress interfaces. These runtime changes do not rewrite YAML. Direct `tc`
inspection remains available, e.g.:

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

The testbed plumbing (dualpi2 marking CE at the bottleneck) is necessary but
not sufficient for L4S results: the QUIC transport must send ECT(1) and react
to CE. Set `l4s_ce_target` to a value in `(0, 1)` on a relay to enable mvfst
L4S ECN for connections accepted by that relay; omitting it leaves ECN
disabled. Transport-level ECN counters are not yet exported by moqx.
