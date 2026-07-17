# TopNFilter Logic — Detailed Explanation

## Overview

`TopNFilter` is a **passive observer** that sits in the object delivery pipeline and watches for property values in object extensions. It doesn't make decisions; it feeds observations to `PropertyRanking`, which *does* make the selection decisions and fires callbacks that the relay uses to start/stop forwarding tracks to each subscriber.

The system has three layers:

1. **TopNFilter** (passive observer): scans extensions for property values, throttles activity notifications
2. **PropertyRanking** (the ranking engine): maintains descending-sorted tracks by `(value, arrivalSeq)`, computes top-N sets per-subscriber, fires selection callbacks
3. **MoqxRelay callbacks** (the action layer): wires/unwires forwarders to sessions based on PropertyRanking decisions

---

## Layer 1: TopNFilter — the observer

### What it does

**Per-track, per-observer:**

```cpp
// In TopNFilter::registerObserver() (called by MoqxRelay::publish()):
observers_[propertyType] = ObserverEntry{
    .lastSeenValue = std::nullopt,
    .observer = {
        .onValueChanged = [ranking, ftn](uint64_t v) { ranking->updateSortValue(ftn, v); },
        .onActivity = [ranking]() { ranking->sweepIdle(); },
        .onTrackEnded = [ranking, ftn]() { ranking->removeTrack(ftn); },
    }
};
```

**For every object** that flows through the filter (publish→filter→downstream):

```cpp
TopNFilter::checkProperties(const moxygen::Extensions& extensions) {
  if (observers_.empty()) return;  // Fast path
  
  auto now = std::chrono::steady_clock::now();
  
  for (auto& [propertyType, entry] : observers_) {
    auto valueOpt = extensions.getIntExtension(propertyType);
    if (!valueOpt) continue;  // Property not present; skip
    
    uint64_t value = *valueOpt;
    
    // Update activity timestamp (cheap pointer write)
    if (activityTarget_) {
      *activityTarget_ = now;  // Raw pointer write, O(1)
    }
    
    // VALUE CHANGED? Fire onValueChanged callback
    if (!entry.lastSeenValue || *entry.lastSeenValue != value) {
      entry.lastSeenValue = value;
      entry.observer.onValueChanged(value);  // Calls ranking->updateSortValue(ftn, value)
    }
    
    // ACTIVITY THROTTLE PASSED? Fire onActivity
    // Throttle prevents firing on every object; batches ~every activityThreshold_
    if (activityThresholdAllows && entry.observer.onActivity) {
      entry.observer.onActivity();  // Calls ranking->sweepIdle()
    }
  }
}
```

**Entry/exit points:**

- **On publish**: `MoqxRelay::publish()` calls `topNFilter->registerObserver(propertyType, observer)` with callbacks wired to the ranking.
- **On every object**: `TopNFilter` sits in the consumer chain, intercepts `beginSubgroup`, `objectStream`, `datagram` via [TopNSubgroupConsumer](src/relay/TopNFilter.cpp#L145), calls `checkProperties()`.
- **On publish end**: `TopNFilter::publishDone()` calls `notifyTrackEnded()` → `onTrackEnded` callback → `ranking->removeTrack(ftn)`.

### Key insight: two-stop throttle

TopNFilter throttles `onActivity` per-track per-filter (via `activityThreshold_`), avoiding excessive callbacks. PropertyRanking then throttles globally (via `sweepThrottle_` in `sweepIdle()`). So even if 100 tracks fire `onActivity` simultaneously, `sweepIdle()` runs at most once per `sweepThrottle_` window.

---

## Layer 2: PropertyRanking — the ranking engine

### State structure

The ranking engine maintains:

```cpp
class PropertyRanking {
  // Descending-sorted map: (value, arrivalSeq) → track entry
  std::map<RankKey, RankedEntry, std::greater<RankKey>> rankedTracks_;
  
  // O(1) lookup: track name → iterator + cached rank
  folly::F14FastMap<FullTrackName, RankIndex> trackIndexByName_;
  
  // Per-N-value groups (e.g., one group for N=10, one for N=20)
  folly::F14FastMap<uint64_t, TopNGroup> topNGroups_;
  
  // Per-session within a TopNGroup (for self-exclusion tracking)
  struct TopNGroup {
    uint64_t maxSelected;  // N for this group
    folly::F14FastMap<MoQSession*, SessionInfo> sessions;  // Per-session state
    folly::F14FastMap<FullTrackName, TrackState> trackStates;  // Selected/Deselected
    std::deque<FullTrackName> deselectedQueue;  // Recently evicted tracks, cheap reselection
  };
  
  // Per-publisher tracking (for self-exclusion)
  folly::F14FastMap<MoQSession*, size_t> publisherTrackCount_;  // How many tracks does each session publish?
};
```

**RankKey logic (descending order):**

```cpp
struct RankKey {
  uint64_t value;        // Property value (higher = better)
  uint64_t arrivalSeq;   // Tie-breaker (lower = earlier = wins)
  
  bool operator<(const RankKey& other) const {
    if (value != other.value) return value < other.value;
    return arrivalSeq > other.arrivalSeq;  // Note: > for descending
  }
};

// std::map<RankKey, RankedEntry, std::greater<RankKey>>
// => iterates descending: highest value first, earliest arrival first on ties
// Rank 0 = best, rank 1 = next best, etc.
```

### Update path: updateSortValue (the hot path)

When TopNFilter fires `onValueChanged(value)`, it calls:

```cpp
PropertyRanking::updateSortValue(const FullTrackName& ftn, uint64_t value) {
  // 1. Lookup track
  auto& entry = trackIndexByName_[ftn];
  RankKey oldKey = entry.rankIter->first;
  
  // 2. Early exit: no change in value
  if (oldKey.value == value) return;
  
  // 3. Capture old rank (before modifying the map)
  uint64_t oldRank = getCachedRank(ftn);  // O(1) after rebuild
  
  // 4. Move the entry in the map to its new position
  RankKey newKey{value, oldKey.arrivalSeq};
  auto rankedEntry = std::move(entry.rankIter->second);  // Move ownership
  rankedTracks_.erase(entry.rankIter);  // Erase old position
  auto [newIter, _] = rankedTracks_.emplace(newKey, std::move(rankedEntry));  // Insert new
  entry.rankIter = newIter;  // Update lookup table
  
  // 5. Compute new rank
  uint64_t newRank = getCachedRank(ftn);  // O(1)
  
  // FAST PATH: Does the move cross any selection boundary?
  if (!crossesThreshold(oldRank, newRank)) {
    XLOG(DBG5) << "Fast path: rank " << oldRank << " -> " << newRank 
               << " (no threshold crossed)";
    return;  // Exit early, no callback firing
  }
  
  // SLOW PATH: Crossed a threshold, recompute all TopNGroup selections
  recomputeTopNGroups(ftn, oldRank, newRank);
  
  // Opportunistic idle sweep after recompute
  sweepIdle();
}
```

**crossesThreshold logic (O(log G)):**

```cpp
bool PropertyRanking::crossesThreshold(uint64_t oldRank, uint64_t newRank) const {
  if (topNGroups_.empty()) return false;
  
  // Build effective threshold: max(all N values) + maxDeselected
  // or for publishers with self-exclusion: max(N + self-track-count)
  uint64_t effectiveThreshold = std::max(selectionThreshold_, 
                                         publisherExtendedThreshold_);
  
  // Fast exit: both ranks outside any group's interest
  if (oldRank >= effectiveThreshold && newRank >= effectiveThreshold) {
    return false;  // Move is below the visible window
  }
  
  // Check if track moved between "in shared top-N" vs "out of top-N"
  bool oldInAnyTopN = oldRank < selectionThreshold_;
  bool newInAnyTopN = newRank < selectionThreshold_;
  if (oldInAnyTopN != newInAnyTopN) {
    return true;  // Definitely crosses a boundary
  }
  
  // O(log G) check: is there an N-threshold between oldRank and newRank?
  auto it = std::upper_bound(sortedThresholds_.begin(), 
                              sortedThresholds_.end(), 
                              std::min(oldRank, newRank));
  if (it != sortedThresholds_.end() && *it <= std::max(oldRank, newRank)) {
    return true;  // Rank move crosses an N boundary (e.g., rank 5→2 crosses N=3)
  }
  
  // Publisher check: for self-exclusion, even if rank move doesn't cross an N,
  // it might affect a publisher-subscriber's personal waterline.
  // Example: rank 8→9, N=10, but two publishers. First publisher published
  // tracks at ranks 0, 1, 8, 9 (four self-tracks). For that publisher,
  // the effective N is 10+4=14. Rank 8→9 might shift which non-self tracks
  // they see (waterline).
  if (!publisherTrackCount_.empty()) {
    // Is the move within the publisher-extended window?
    if (std::min(oldRank, newRank) < publisherExtendedThreshold_) {
      return true;
    }
  }
  
  return false;
}
```

**Example: Fast path (value drift, no recompute)**

```
Track A at rank 5, value=50, N=10, N=20, N=50
  onValueChanged(51):
    oldRank = 5, newRank = 4 (moved up one position)
    crossesThreshold(5, 4)?
      min(5,4)=4, max(5,4)=5, both < selectionThreshold_=20+0=20 ✓
      sortedThresholds_=[10, 20, 50]
      upper_bound(4)=10 (first threshold > 4)
      Is 10 <= 5? No.
      → return false  (no threshold crossed)
    Fast-path return, no callback firing! ✓
    
  Rank still 5 for A's view, just with a higher value. No selection change.
```

**Example: Slow path (crossing N boundary, fires callbacks)**

```
Track A at rank 10, value=50
  onValueChanged(100):  # Big jump
    oldRank = 10, newRank = 5 (jumped up)
    crossesThreshold(10, 5)?
      min(10,5)=5, max(10,5)=10
      sortedThresholds_=[10, 20, 50]
      upper_bound(5)=10
      Is 10 <= 10? Yes! → return true  (crosses the N=10 boundary)
    recomputeTopNGroups(A, 10, 5)
      For TopNGroup N=10:
        wasInTopN = (10 < 10) = false
        nowInTopN = (5 < 10) = true  # Track entered top-10!
        Calls demoteTrackAtRank(10, group)  # Push out the old rank-10 track
        Fires onSelected_ callbacks for viewers and publishers
```

### recomputeTopNGroups: track entering/leaving top-N

When a track's rank crosses an N boundary, the ranking fires callbacks:

```cpp
void PropertyRanking::recomputeTopNGroups(
    const FullTrackName& ftn,
    uint64_t oldRank,
    uint64_t newRank
) {
  for (auto& [n, topNGroup] : topNGroups_) {
    bool wasInTopN = oldRank < n;
    bool nowInTopN = newRank < n;
    
    if (wasInTopN != nowInTopN) {
      // Track entered or left this group's top-N
      
      if (nowInTopN) {
        // ENTERING: mark as Selected, fire onSelected_ for all sessions
        topNGroup.trackStates[ftn] = TrackState::Selected;
        
        for (auto& [session, info] : topNGroup.sessions) {
          if (isPublisher(session)) {
            // Publisher-subscriber: reconcile their personal top-N
            reconcilePublisherSelection(info, n, session);
          } else {
            // Viewer: batch notify
            viewerBatch.push_back(session);
          }
        }
        
        if (!viewerBatch.empty()) {
          onBatchSelected_(ftn, viewerBatch);  # Send to relay: "start forwarding ftn to these viewers"
        }
        
        // Displace: the track now at rank N was previously selected, push it down
        demoteTrackAtRank(n, topNGroup);
        
      } else {
        // LEAVING: track fell out of top-N
        demoteTrack(topNGroup, ftn);  // Mark Deselected, add to queue
        
        // Promote: find the best un-selected track and move it up
        promoteNextAvailableTrack(topNGroup, ftn);
      }
    }
  }
}
```

**Callbacks fired:**

- **`onSelected_(ftn, session, forward)`**: Called per-session. Relay's callback: wire ftn's forwarder into session's subscriber list.
- **`onBatchSelected_(ftn, sessions)`**: Called for viewers in a TopNGroup. Relay's callback: same as above, but batches multiple sessions for efficiency (all have same N).
- **`onEvicted_(ftn, session)`**: Called when track left a session's selection. Relay's callback: unwire ftn's forwarder from session.

### Self-exclusion: reconcilePublisherSelection

When a publisher-subscriber's waterline moves (or a new publisher joins), this method computes which non-self tracks they should receive:

```cpp
void PropertyRanking::reconcilePublisherSelection(
    SessionInfo& info,
    uint64_t maxSelected,
    const std::shared_ptr<moxygen::MoQSession>& session
) {
  // 1. Compute the Nth non-self track (the waterline key)
  info.waterlineKey = computeWaterlineKey(session, maxSelected);
  
  // 2. Determine what should be delivered
  folly::F14FastSet<FullTrackName> nowSelected;
  for (const auto& [key, entry] : rankedTracks_) {
    // Is this a non-self track?
    if (entry.publisherRaw != session.get() && 
        // Is it at or above the waterline?
        (!info.waterlineKey || key >= *info.waterlineKey)) {
      nowSelected.insert(entry.ftn);
    }
  }
  
  // 3. Evict: tracks that were delivered but shouldn't be anymore
  for (const auto& ftn : info.selectedTracks) {
    if (nowSelected.count(ftn) == 0) {
      onEvicted_(ftn, session);  // Relay: unwire this track
    }
  }
  
  // 4. Select: tracks that should be delivered now but weren't before
  for (const auto& ftn : nowSelected) {
    if (info.selectedTracks.count(ftn) == 0) {
      onSelected_(ftn, session, info.forward);  // Relay: wire this track
    }
  }
  
  info.selectedTracks = nowSelected;  // Update state for next time
}
```

**computeWaterlineKey logic:**

```cpp
std::optional<RankKey> PropertyRanking::computeWaterlineKey(
    const std::shared_ptr<moxygen::MoQSession>& session,
    uint64_t maxSelected
) const {
  uint64_t nonSelfCount = 0;
  for (const auto& [key, entry] : rankedTracks_) {
    if (entry.publisherRaw != session.get()) {  // Non-self?
      nonSelfCount++;
      if (nonSelfCount == maxSelected) {
        return key;  // Nth non-self track found
      }
    }
  }
  return std::nullopt;  // Fewer than N non-self tracks exist
}
```

**Example: Self-exclusion with N=3**

```
Global ranking:
  Rank 0: Alice's track (Alice is publisher-subscriber, N=3)
  Rank 1: Bob's track
  Rank 2: Carol's track
  Rank 3: Dave's track
  Rank 4: Eve's track

Alice's view (N=3, excluding own tracks):
  computeWaterlineKey(Alice, 3):
    - Skip rank 0 (Alice's own track)
    - Count non-self: Bob (1), Carol (2), Dave (3) ← waterlineKey = RankKey(Dave's value)
    - Return RankKey(Dave's value)
  
  nowSelected = {Bob, Carol, Dave}
  
If Alice publishes another track that enters rank 2:
  Global ranking becomes:
    Rank 0: Alice's old track
    Rank 1: Alice's new track (enters top-3 globally)
    Rank 2: Bob's track
    Rank 3: Carol's track
    Rank 4: Dave's track
    ...
  
  Alice's view (recomputed):
    computeWaterlineKey(Alice, 3):
      - Skip rank 0 (self)
      - Skip rank 1 (self)
      - Count non-self: Bob (1), Carol (2), Dave (3) ← waterlineKey stays RankKey(Dave's value)
      - Return RankKey(Dave's value)
    
    nowSelected = {Bob, Carol, Dave}  (same as before)
    
    Delta: nothing changes for Alice! She still gets Bob, Carol, Dave.
    But globally, Alice's new track pushed Eve down.
```

### Idle eviction: sweepIdle

Periodically (throttled by `sweepThrottle_` in PropertyRanking and `activityThreshold_` in TopNFilter), PropertyRanking scans selected tracks for idle ones:

```cpp
void PropertyRanking::sweepIdle() {
  if (idleTimeout_.count() == 0) return;  // Feature disabled
  
  auto now = std::chrono::steady_clock::now();
  
  // Global throttle: skip if called too recently
  if (lastSweepTime_ && now - *lastSweepTime_ < sweepThrottle_) {
    return;
  }
  lastSweepTime_ = now;
  
  for (auto& [n, topNGroup] : topNGroups_) {
    // Snapshot selected tracks (deselecting mutates the map)
    std::vector<FullTrackName> selected;
    for (const auto& [ftn, state] : topNGroup.trackStates) {
      if (state == TrackState::Selected) {
        selected.push_back(ftn);
      }
    }
    
    for (const auto& ftn : selected) {
      auto lastActivity = getLastActivity_(ftn);  // Relay callback: read timestamp
      
      // Epoch (default time_point{}) means never published
      bool neverPublished = (lastActivity == std::chrono::steady_clock::time_point{});
      
      if (!neverPublished && now - lastActivity <= idleTimeout_) {
        continue;  // Still active, keep it
      }
      
      // Track is idle or never published → demote
      XLOG(DBG4) << "Idle eviction: " << ftn << (neverPublished ? " [never published]" : "");
      
      demoteTrack(topNGroup, ftn);  // Move to deselected queue
      promoteNextAvailableTrack(topNGroup, ftn);  // Replace with best available
    }
  }
}
```

**The activity timestamp source:**

TopNFilter writes the timestamp every time a property-matched object arrives:

```cpp
// In TopNFilter::checkProperties():
if (activityTarget_) {
  *activityTarget_ = now;  // Raw pointer to per-track timestamp storage
}
```

The relay sets `activityTarget_` to point to a per-track slot in [SubscriptionRegistry::TopNView](src/SubscriptionRegistry.h#L122):

```cpp
struct TopNView {
  std::shared_ptr<MoQForwarder> forwarder;
  std::shared_ptr<TopNFilter> topNFilter;
  std::chrono::steady_clock::time_point lastObjectTime;  ← written by TopNFilter
};
```

---

## Layer 3: MoqxRelay callbacks — the action layer

When PropertyRanking fires callbacks, the relay wires/unwires forwarders:

```cpp
// From MoqxRelay::publish():
topNFilter->registerObserver(
    propertyType,
    PropertyObserver{
        .onValueChanged = [ranking, ftn](uint64_t value) {
          ranking->updateSortValue(ftn, value);  // Feed property changes to ranking
        },
        .onTrackEnded = [ranking, ftn]() {
          ranking->removeTrack(ftn);  // Ranking cleanup on publish end
        },
        .onActivity = [ranking]() {
          ranking->sweepIdle();  // Trigger idle sweep
        }
    }
);

// Callbacks fired by PropertyRanking (wired in MoqxRelay::getOrCreateRanking()):
ranking->setOnSelected([this](const FullTrackName& ftn, 
                              const std::shared_ptr<MoQSession>& session, 
                              bool forward) {
  // Relay action: wire ftn's forwarder to session's subscriber list
  onTrackSelected(ftn, session, forward);
});

ranking->setOnEvicted([this](const FullTrackName& ftn, 
                             const std::shared_ptr<MoQSession>& session) {
  // Relay action: unwire ftn's forwarder from session's subscriber list
  onTrackEvicted(ftn, session);
});

ranking->setOnBatchSelected([this](const FullTrackName& ftn,
                                   const std::vector<std::pair<...>>& sessions) {
  // Relay action: wire ftn to all sessions in batch (efficient)
  for (const auto& [session, forward] : sessions) {
    onTrackSelected(ftn, session, forward);
  }
});
```

### onTrackSelected: wiring the forwarder

```cpp
void MoqxRelay::onTrackSelected(
    const FullTrackName& ftn,
    std::shared_ptr<MoQSession> session,
    bool forward
) {
  // Find or subscribe to upstream
  auto forwarder = registry_.getForwarder(ftn);
  if (!forwarder) {
    XLOG(ERR) << "onTrackSelected: no forwarder for " << ftn;
    return;
  }
  
  // Start a publish to this session with the forwarder as consumer
  co_withExecutor(exec, publishToSession(session, forwarder, forward,
                                         /*trackFilterSubscriber=*/true)).start();
}

void MoqxRelay::publishToSession(
    std::shared_ptr<MoQSession> session,
    std::shared_ptr<MoQForwarder> forwarder,
    bool forward,
    bool trackFilterSubscriber
) {
  // Add this session as a subscriber to the forwarder
  auto subscriber = forwarder->addSubscriber(session, forward);
  subscriber->pinned = !trackFilterSubscriber;  // Can be evicted by PropertyRanking
  
  // Send PUBLISH message to session
  auto pubInitial = session->publish(subscriber->getPublishRequest(), subscriber);
  // ... wire up subscriber->trackConsumer after PUBLISH_OK ...
}
```

---

## End-to-end example: audio stream top-3 scenario

**Setup:**
- Relay has 5 audio streams, each publisher embeds `PropertyType=0x1` (audio level) in object extensions
- Subscriber Alice connects with `TRACK_FILTER(propertyType=0x1, maxSelected=3)`

**Timeline:**

```
T=0: All tracks register at PropertyRanking
  - Dave:   level=90 → rank 0 (highest)
  - Carol:  level=80 → rank 1
  - Bob:    level=70 → rank 2
  - Alice2: level=60 → rank 3 (outside top-3)
  - Eve:    level=50 → rank 4 (outside top-3)

T=1: Alice (the subscriber, not the track) sends SUBSCRIBE_NAMESPACE with TRACK_FILTER
  MoqxRelay::subscribeNamespace():
    - Parses TRACK_FILTER(propertyType=0x1, maxSelected=3)
    - Calls ranking->addSessionToTopNGroup(3, Alice-session, forward=1)
  
  PropertyRanking::addSessionToTopNGroup(3, Alice, forward=1):
    - Creates TopNGroup for N=3 if needed
    - Adds Alice to it
    - Checks isPublisher(Alice)? No, she's a viewer
    - Loops over top-3 tracks: Dave, Carol, Bob
    - For each, fires onSelected_(ftn, Alice, forward=1)
  
  Result: Alice immediately receives PUBLISH messages for Dave, Carol, Bob

T=2: Objects arrive from all tracks
  - Dave publishes object with level=90: TopNFilter sees no value change (still 90)
  - Carol publishes object with level=80: same, no change
  - Bob publishes object with level=70: same, no change
  - Alice2 publishes object with level=60: TopNFilter fires onValueChanged(60)
    → ranking->updateSortValue(Alice2-track, 60)
    → oldRank=3, newRank=3, no threshold crossed (fast path)
  - Eve publishes object with level=50: same, no change

T=3: Carol's audio decreases!
  - Carol publishes object with level=55: TopNFilter fires onValueChanged(55)
    → ranking->updateSortValue(Carol-track, 55)
    → oldRank=1, new rank=4 (Carol moved down)
    → crossesThreshold(1, 4)? YES! (rank 1 leaves top-3)
    → recomputeTopNGroups(Carol-track, 1, 4)
    
    For TopNGroup N=3:
      wasInTopN = (1 < 3) = true
      nowInTopN = (4 < 3) = false  ← Carol left top-3!
      
      demoteTrack(Carol-track):  Add to deselected queue
      promoteNextAvailableTrack(): Find rank-3 track (Alice2)
        - Alice2 was Deselected, now mark Selected
        - Fire onSelected_(Alice2-track, Alice, forward=1)
  
  Result: Alice receives PUBLISH message for Alice2-track
          Alice stops receiving Carol-track (old subscriber unsubscribed)

T=4: Bob recovers!
  - Bob publishes object with level=85: TopNFilter fires onValueChanged(85)
    → ranking->updateSortValue(Bob-track, 85)
    → oldRank=2, newRank=1 (Bob moved up, Carol is now rank 2 or lower)
    → crossesThreshold(2, 1)? YES! (rank 1 is an N-boundary, assuming other groups)
    → recomputeTopNGroups(Bob-track, 2, 1)
    
    For TopNGroup N=3:
      wasInTopN = (2 < 3) = true
      nowInTopN = (1 < 3) = true  ← Bob stayed in top-3
      
      No demote/promote. Bob's rank shifted, but top-N set unchanged.

T=5: No one publishes for 30 seconds
  TopNFilter throttled onActivity fires periodically:
    → ranking->sweepIdle()
  
  PropertyRanking::sweepIdle():
    - Check Alice2-track: lastActivity = T=3, now - T=3 > idleTimeout_ (30s)
    - Alice2 is idle! Demote it, promote next available (Carol or Eve)
    
  Result: Alice stops receiving Alice2-track, might receive Carol or Eve instead

T=6: Alice herself publishes a track!
  Dave (the track, not the person) becomes Alice (the publisher), sends PUBLISH
  
  MoqxRelay::publish():
    - Creates new track: Alice-track
    - Calls ranking->registerTrack(Alice-track, ...)
    - Checks if Alice (session) already subscribed to N=3 group
    - Calls reconcilePublisherInAllGroups(Alice-session)
    
  PropertyRanking::reconcilePublisherInAllGroups(Alice):
    - Alice is now a publisher (owns Alice-track)
    - Computes waterline: top-3 non-self tracks
      - Dave (rank 0, not self) ✓
      - Bob (rank 1, not self) ✓
      - Rank 2 is now Alice-track (self) ✗
      - Alice2 (rank 3, not self) ✓ ← 3rd non-self
      → waterlineKey = RankKey(Alice2)
    - nowSelected = {Dave, Bob, Alice2}
    - Evict tracks no longer in personal top-3
    - Select new tracks
    
  Result: Alice's personal top-3 are Dave, Bob, Alice2 (not her own track)
```

---

## Summary: the full flow

```
Publisher sends object
    ↓
TopNFilter::checkProperties()
    - Scans extensions for property values
    - Fires onValueChanged if value changed
    - Fires onActivity if throttle allows (but doesn't call sweepIdle here; callback does)
    ↓
onValueChanged callback (wired by MoqxRelay)
    → ranking->updateSortValue(ftn, newValue)
    ↓
PropertyRanking::updateSortValue()
    - Moves track in sorted map
    - Checks crossesThreshold() (O(log G))
    - If fast path: returns early, no callbacks
    - If slow path: calls recomputeTopNGroups()
    ↓
PropertyRanking::recomputeTopNGroups()
    - For each TopNGroup N:
        - If track entered top-N: calls onSelected_ for all sessions, demotes rank-N track
        - If track left top-N: demotes track, promotes best available
    ↓
onSelected_ / onEvicted_ callbacks (wired by MoqxRelay)
    → MoqxRelay::onTrackSelected() / onTrackEvicted()
    → publishToSession() / unsubscribe()
    ↓
Session receives PUBLISH message (starts receiving objects) 
or UNSUBSCRIBE (stops receiving objects)
```

The relay doesn't micro-manage which objects go where — it manages which **tracks** each **subscriber** receives, and the MoQForwarder handles the fanout for each track to all its subscribers.
