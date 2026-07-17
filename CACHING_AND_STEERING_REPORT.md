# Caching & Content Steering in moqx — Detailed Report

## TL;DR

> **Is it just fanout, or can relays choose what to send to which relay/subscriber?**

It is **both**, but the "steering" is **subscriber-driven selection at the subgroup-object level, not relay-initiated routing decisions**. Concretely:

- **Caching** is a full-featured, hierarchical **object cache** (`MoqxCache`) with LRU eviction, TTLs, byte-level dedup, upstream miss-fetching, and concurrent-request coalescing.
- **Default delivery is fanout**: one upstream subscription per track, fanned out to N downstream subscribers via moxygen's `MoQForwarder`.
- **On top of fanout there is genuine content selection**: the `TRACK_FILTER` / `PropertyRanking` / `TopNFilter` machinery lets the relay deliver only the **top-N tracks** (ranked by a property value embedded in object extensions, e.g. audio level) to each subscriber — and the set is computed **per-subscriber** (with self-exclusion for publisher-subscribers).
- **Relay-to-relay topology is a static upstream chain, not adaptive steering.** A relay has at most **one configured upstream per service**, plus a symmetric peering handshake. There is **no** weight-based / load-based / latency-based peer selection in the code today — `MeshProvider`/`ChainedProvider` (which *would* do routing) exist only as **design sketches**, not as implementations.

So: relays do not dynamically *choose which peer to send to*. But within a delivery, a relay does *choose which tracks/objects each subscriber gets* (top-N ranking + dedup + cache range-serving). That is the "steering" that actually exists.

---

## 1. The relay core

`MoqxRelay` is a hard fork of moxygen's `MoQRelay`. It implements both `Publisher` and `Subscriber` and owns:

- a **namespace tree** (`NamespaceTree`) — who published which namespaces/tracks, who subscribed to which prefixes;
- a **subscription registry** (`SubscriptionRegistry`) — active subscriptions and their `MoQForwarder` fanout engines;
- a **cache** (`MoqxCache`);
- an optional **upstream provider** (`UpstreamProvider`).

The data-plane fanout engine itself (`MoQForwarder`) is reused **as-is from moxygen**.

---

## 2. Caching

### 2.1 What gets cached and how

`MoqxCache` is an in-memory object cache keyed by:
```
FullTrackName → CacheTrack → CacheGroup → CacheEntry
```

Each `CacheEntry` stores:
- payload (folly::IOBuf chain)
- status (complete, part of stream, datagram, etc.)
- extensions (metadata)
- byte count
- completeness flag
- `cachedAt` timestamp

The cache sits in the delivery pipeline as a **writeback filter**: on a subscribe, `getSubscribeWriteback()` wraps the downstream consumer so every object flowing through is *both* forwarded *and* written to cache.

### 2.2 Eviction & limits

Three independent eviction axes, all configurable per-service:

| Limit | Config key | Mechanism |
|---|---|---|
| Max tracks | `max_tracks` | track-level LRU (`trackLRU_`) |
| Max groups/track | `max_groups_per_track` | per-track group LRU (`groupLRU`) |
| Max total bytes | `max_cached_mb` | global group LRU (`globalGroupLRU_`), evicts in `min_eviction_kb` batches |
| TTL | `max_cache_duration_s` / `default_max_cache_duration_s` | per-object `cachedAt` + per-track duration; publisher-set durations are clamped to the max |

**TTL is interesting:** a publisher can request a cache duration via **object extensions**; the relay honors it but clamps it to `maxAllowedCacheDuration_`, falling back to a default. `default_max_cache_duration_s: 0` means **opt-in-only** — don't cache unless the publisher explicitly asks.

### 2.3 Cache on FETCH — the hierarchical / miss-handling part

This is where caching gets sophisticated. `MoqxRelay::fetch()` routes through `cache_->fetch(fetch, consumer, upstream)`. The cache:

1. **Serves cached objects** in the requested range directly;
2. **For gaps**, issues one-or-more upstream FETCHes (`fetchUpstream`), writes results back to cache, and forwards to the consumer;
3. **Coalesces concurrent upstream requests** for the same object range (interval map `fetchesInProgress`) so two simultaneous clients don't double-fetch;
4. **Tracks known gaps** (`LocationIntervalSet gaps`) so it doesn't repeatedly re-fetch objects that genuinely don't exist.

The design intent (`design/miss-handler.md`) is a **chain** of `MissHandler`s (memory → disk → remote → origin), and even a `FailoverMissHandler` that tries multiple upstreams in sequence. **In the current code the cache takes a single `std::shared_ptr<Publisher> upstream`** — the full chained-`MissHandler` abstraction is designed but not yet the live interface. So today: **one upstream, with cache in front.**

### 2.4 Byte-level dedup (designed, "gummy-bear")

`design/gummy-bear.md` describes a multi-publisher deduplicator: when several publishers send the *same* track, the cache's per-object `bytesReceived` state is used to truncate duplicate byte prefixes so each byte is forwarded once, with seamless failover if one publisher dies mid-object. This leans on the same `CacheEntry`/object-state tracking. (Design doc + cache hooks present; treat the standalone `Deduplicator` as design-stage.)

---

## 3. Delivery: fanout is the baseline

For a normal subscribe (`MoqxRelay::subscribe()`):

- **First subscriber** for a track: relay creates a `MoQForwarder`, subscribes **once** upstream (forcing `forward=1`, latest-object, default group order), and wires the upstream's objects into the forwarder.
- **Subsequent subscribers**: simply `addSubscriber()` to the **existing** forwarder — one upstream pull, many downstream pushes. Classic fanout with dedup of the upstream subscription.

So by default a relay sends **the same track to every subscriber that asked for it**. No per-subscriber content differentiation.

---

## 4. Content steering: the part that is *not* just fanout

The selective layer is the **`TRACK_FILTER`** feature, built from three pieces.

### 4.1 TopNFilter — the observer

Sits in the consumer chain:
```
Publisher → TopNFilter → TerminationFilter → cache → Forwarder
```

(Built by `buildFilterChain()`.)

For every object it calls `checkProperties()`, which scans the object's **extensions** for a registered property type (e.g. audio level). When the value changes it fires `onValueChanged`; throttled activity fires `onActivity` (drives idle eviction). It does **not** decide anything itself — it feeds the ranking engine.

### 4.2 PropertyRanking — the ranking engine

Maintains a descending-sorted map of all tracks by `(value, arrivalSeq)`. Subscribers that sent a `TRACK_FILTER` with `maxSelected = N` are grouped into a **TopNGroup**. The engine fires:

- `onSelected` → relay calls `onTrackSelected()` → `publishToSession(...)` wires that track into *that subscriber's* forwarder;
- `onEvicted` → relay tears that track's delivery to that subscriber down.

When a track's property value drifts past an N-boundary, the relay **starts/stops forwarding specific tracks to specific subscribers** — that is real content steering at the track granularity.

### 4.3 Self-exclusion / per-subscriber waterline

A session that is *both* a publisher and a subscriber doesn't get its own tracks back. Each publisher-subscriber gets a **personal waterline** = the Nth *non-self* track in the global ranking. This means **two subscribers with the same N can receive different sets of tracks** depending on what they themselves publish. That is per-subscriber steering, not fanout.

### 4.4 Idle eviction

A track that's ranked into the top-N but goes silent is demoted (`sweepIdle()`), and the next-best track is promoted — so the selected set adapts to *activity*, not just static value.

**Net:** the relay chooses *which tracks each subscriber receives*, recomputed live as property values and activity change. This is the system's actual "content steering."

---

## 5. Relay-to-relay topology

### 5.1 Upstream chaining (static)

Per service you can configure **one** `upstream.url`. `MoqxRelayContext::initUpstreams()` builds one `UpstreamProvider` per service that has an upstream, connects eagerly, and reconnects with exponential backoff (1s→60s). The `UpstreamProvider` presents itself locally as both Publisher and Subscriber and **forwards** subscribe/fetch/publish to the remote.

### 5.2 Peering handshake (symmetric discovery, not routing)

On connect, a relay issues a wildcard `subscribeNamespace(*, BOTH)` carrying a **relay auth token** with its `relay_id`. When a relay *receives* such a token-bearing subNs, it **reciprocates** with its own, tags the peer by ID to **suppress echo loops**, and registers the peer as a normal namespace subscriber. This is **namespace discovery propagation between relays** — it's how a downstream relay learns what an upstream can serve. It is *not* a decision about which peer to route a given request to.

### 5.3 What's fanout vs fallback between relays

| Operation | Pattern |
|---|---|
| `subscribe` / `fetch` / `trackStatus` | **Fallback**: local, then the single upstream |
| `subscribeNamespace` / `publishNamespace` / `publish` | **Fan-out**: local AND upstream |

### 5.4 What does *not* exist yet

The design doc sketches `MeshProvider` ("Select peer based on weights") and `ChainedProvider` (try upstream1, fall through to upstream2) and a `FailoverMissHandler`. **None of these are implemented.** There is no routing table, no peer-weighting, no load/latency-based selection in `src/`. A relay cannot, today, decide "send track X to peer A and track Y to peer B." Inter-relay content flow follows the static upstream link + namespace-discovery handshake only.

---

## 6. Summary: what actually steers content

| Capability | Status |
|---|---|
| Fanout of one track to many subscribers | ✅ default (`MoQForwarder`) |
| Cache with LRU/TTL/byte limits | ✅ `MoqxCache` |
| Cache serving FETCH ranges + upstream miss-fetch + request coalescing | ✅ `MoqxCache::fetch()` |
| Hierarchical/chained caches + multi-upstream failover | 📐 designed, single-upstream in code (`design/miss-handler.md`) |
| Byte-level multi-publisher dedup | 📐 designed (`design/gummy-bear.md`) |
| **Relay chooses which tracks each subscriber gets (top-N)** | ✅ `PropertyRanking` + `TopNFilter` |
| **Per-subscriber differentiation (self-exclusion waterline)** | ✅ `docs/dev/track-filter-ranking.md` |
| **Relay chooses which peer to send which data to (adaptive routing/steering)** | ❌ not implemented (only static single upstream + symmetric discovery) |

**Bottom line:** moqx does far more than blind fanout — it does **subscriber-driven, property-ranked, top-N content selection** with self-exclusion, backed by a real range-serving cache. But it does **not** do relay-side adaptive *content steering between peers*: the relay topology is a static per-service upstream link plus a symmetric namespace-discovery handshake. The "choose which relay gets which data" routing (mesh/weights/failover) lives in design docs, not in the shipping code.

---

## 7. Key files for deeper dives

| Component | Files |
|---|---|
| Core relay | `src/MoqxRelay.h/cpp`, `src/MoqxRelayContext.cpp`, `src/MoqxRelayServer.h/cpp` |
| Cache | `src/MoqxCache.h/cpp` |
| Fanout | moxygen `MoQForwarder` (external; referenced in `src/SubscriptionRegistry.h`) |
| Content selection (TRACK_FILTER) | `src/relay/TopNFilter.h/cpp`, `src/relay/PropertyRanking.h/cpp` |
| Upstream provider | `src/UpstreamProvider.h/cpp` |
| Namespace tree | `src/NamespaceTree.h/cpp` |
| Config schema | `src/config/Config.h`, `config.example.yaml` |
| Design docs | `design/gummy-bear.md` (dedup), `design/miss-handler.md` (cache chaining), `design/black-box.md` (routing abstraction — aspirational) |
| Docs | `docs/dev/track-filter-ranking.md` (full semantics of top-N + self-exclusion) |
