# Router And Link Emulator Architecture

This note describes how moqlab should model network links as the testbed grows
from simple delay/bandwidth shaping into ECN, L4S, AQM, and routing research.

## Current Model

The Containernet backend currently derives topology edges from:

- relay `upstream`
- publisher `connects_to`
- subscriber `connects_to`

For each derived edge, it creates one isolated L2 segment:

```text
node-a -- switch -- node-b
```

The switch is not meant to represent a CDN device. It is a Containernet/Mininet
mechanism for creating a shaped virtual link between two Docker hosts. The link
properties from `links:` are applied with Linux `tc`/`netem` on one endpoint
interface so that `delay_ms: X` means approximately `X` ms one-way delay for
that edge, instead of doubling the delay by shaping both sides.

This model is good for:

- simple bandwidth, delay, jitter, and loss experiments
- application-level CDN relay graphs
- keeping the topology schema free of explicit IP addressing
- avoiding extra routing state when the experiment does not study IP routing

## Why A Router Or Link Emulator May Be Needed

For ECN, L4S, and AQM work, the queue location matters. If `tc` is attached to
a relay container's egress interface, the relay container effectively owns the
bottleneck queue. That can be acceptable for simple link emulation, but it is a
weaker model for experiments where the network queue itself is the subject.

A cleaner future model is:

```text
node-a -- link-emulator/router -- node-b
```

The link emulator or router owns the bottleneck queue:

```text
node-a -> router -> [AQM/ECN/L4S queue] -> node-b
```

This separates application behavior from network behavior. CDN relays remain
application containers, while the network node owns queueing, marking,
dropping, and forwarding behavior.

## Recommended Direction

Keep the current switch-based model for baseline Containernet runs and simple
link shaping. Add a new abstraction when ECN, L4S, AQM, or underlay routing
becomes a first-class research target.

Recommended long-term roles:

- CDN relays, publishers, subscribers: application containers.
- Link emulator/router nodes: network containers or namespaces that own `tc`
  qdiscs, ECN marking, L4S configuration, AQM policy, and forwarding behavior.
- Topology schema: continue to describe logical application edges and shaped
  links without requiring users to write raw IP addresses.

## Routing Scope

There are two different meanings of routing in this testbed:

Application-level relay routing:

```text
subscriber chooses relay-b instead of relay-c for a namespace or track
```

This does not require IP routers. It can be modeled in the relay graph and in
the generated moqx configuration.

Network-level IP routing:

```text
relay-a -> r1 -> r2 -> relay-b
```

This requires forwarding nodes, subnets, routes, and clear bottleneck
placement. It should use the future link-emulator/router abstraction rather
than the current per-edge switch-only model.

## Design Principles

- Do not expose fixed IP addresses in the user-facing topology schema unless a
  future requirement proves it is unavoidable.
- Keep CDN relay behavior and network queue behavior separate.
- Make bottleneck placement explicit for ECN, L4S, and AQM experiments.
- Preserve the simple switch-based path for experiments that only need
  bandwidth, delay, jitter, and loss.
- Prefer a named link or network abstraction before adding complex scenario
  actions such as `set_ecn`, `set_l4s`, or AQM policy changes.

## Possible Future Shape

One possible future schema direction:

```yaml
links:
  - name: edge-relay-a-relay-b
    from: relay-a
    to: relay-b
    emulator: router
    bandwidth_mbps: 50
    delay_ms: 20
    aqm: dualpi2
    ecn: true
    l4s: true
```

The exact schema should be decided when the first ECN/L4S/AQM scenario is
implemented. Until then, `links:` should stay simple and continue to represent
the real topology edges derived from `upstream` and `connects_to`.
