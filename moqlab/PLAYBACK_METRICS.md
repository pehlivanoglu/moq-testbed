# Native Playback Simulation And Live Metrics

This document is the implementation report for chain-correct native playback
simulation and the shared media-subscriber metrics shown by the moqlab
visualizer. It is updated with each implementation checkpoint.

## Goals

- Simulate AV1 spatial-SVC decode and playback in native `mlmsub` clients
  without decoding pixels or launching FFmpeg.
- Preserve AV1-SVC dependency-chain correctness during stalls and switches.
- Export comparable live metrics from native and Chrome media subscribers.
- Read metrics cheaply in large Containernet topologies: one selected node,
  one container exec, once per second.

Non-goals are codec CPU modelling, rendered-pixel validation in native clients,
bandwidth-estimation ABR, persistent metric storage, and metrics for text
subscribers.

## Configuration

```yaml
defaults:
  subscriber:
    media_client: native
    native_playback: simulate  # receive | simulate

subscribers:
  load-1:
    kind: media
    connects_to: relay-a
    namespace: msf/clear
    track: video/s2
    minimal_buffer_ms: 200
    target_latency_ms: 300
```

`native_playback` defaults to `receive`, which preserves the existing native
subscriber behavior. `simulate` treats `track` as the maximum target quality
and enables the buffer settings. Chrome subscribers always use real playback
and ignore an inherited native playback default.

## AV1-SVC Design

The simulator assembles one temporal unit from matching group/object IDs and
LOC timestamps across the catalog dependency chain. It maintains decode
validity for every base-to-layer prefix.

- Startup selects the highest complete prefix on an independent unit.
- A missing layer invalidates that prefix and every higher prefix until a
  complete independent unit arrives.
- Downswitch is allowed without an independent unit only to a lower prefix
  whose decode history remained continuous.
- Upswitch is allowed only on a complete independent unit.
- Layer selection is persistent; the simulator never chooses an arbitrary
  layer separately for each frame.

Playback begins after the minimum buffer is available. LOC timestamps plus the
target latency define presentation deadlines. A stall freezes the playhead;
recovery remains at 1x, so the added latency stays observable.

## Metrics Contract

Every media subscriber atomically replaces
`/tmp/moqlab-player-metrics.json` once per second. Unsupported measurements
are JSON `null` rather than estimates.

| Field | Meaning |
|---|---|
| `schema_version` | Contract version, initially `1`. |
| `sampled_at_unix_ms` | Writer wall-clock sample time. |
| `client` / `simulated` | `chrome` or `native`, and whether playback is fake. |
| `state` | Starting, receiving, buffering, playing, stalled, or error. |
| `target_track` / `active_track` | Configured ceiling and currently decoded chain tip. |
| `switch_state` | Stable or waiting for an independent unit. |
| `quality` | Active spatial ID, width, and height. |
| `e2e_latency_ms` | Wall clock minus the last presented LOC timestamp. |
| `player_bitrate_bps` | Encoded bytes admitted to active playback over the trailing second. |
| `receive_bitrate_bps` | Video bytes received over the trailing second. |
| `catalog_bitrate_bps` | Catalog bitrate sum for the active dependency chain. |
| `buffer_level_ms` | Decodable video duration ahead of the playhead. |
| `playback_rate` | Current playback-rate multiplier. |
| `stall_count` / `stall_duration_ms` | Post-start stalls and cumulative duration. |

## Containernet Data Flow

The visualizer validates a selected node against the topology, resolves its
Containernet container as `mn.<node-id>`, and uses Docker's argv-form exec to
read the fixed metrics path. It does not add ports, mounts, sidecars, or an
all-subscriber polling loop. Docker-backend runs register their actual
subscriber container IDs with the same reader.

## Implementation Log

- 2026-08-10: Design fixed. Chose atomic local files, selected-node container
  reads, fixed 1x recovery, and decode-deadline layer switching. Rejected
  per-frame highest-layer selection because it violates the AV1-SVC decode
  chain.
- 2026-08-10: Added YAML inheritance and launch synthesis. Existing native
  subscribers remain receive-only; simulation resolves the existing 200/300 ms
  buffer defaults and receives explicit `mlmsub` flags.
- 2026-08-10: Added the native state machine, LOC frame-marking validation,
  rolling bitrate counters, atomic metrics writer, and deterministic tests for
  startup, downswitch, upswitch, base-layer stalls, recovery, and bounded
  pending state.
- 2026-08-10: Added a typed WARP Player metrics snapshot, Chrome receive/player
  byte counters, stall accounting, and the CDP runner's atomic sampler.
- 2026-08-10: Added the selected-node API and panel. Containernet uses
  `mn.<node-id>` directly; the Docker backend registers returned container IDs.
  Missing, malformed, oversized, and stale samples remain UI states rather
  than topology failures.
- 2026-08-10: Kept the selected-node panel mounted across one-second topology
  refreshes. Metrics now update existing text fields in place, include the
  writer sample time, and retain one decimal place for timing measurements.
  Native latency and buffer tests also verify that both measurements follow
  changes in wall-clock and queued media state.
- 2026-08-10: Corrected fixed-1x native timing. An object-arrival wakeup and a
  per-frame deadline timer replace the 20 ms playback poll. Each temporal unit
  is presented individually at its media-time interval, and genuinely late
  arrivals re-anchor subsequent deadlines so overdue units cannot be drained
  in a burst. Buffer level is measured from a continuously advancing fake
  playhead, which freezes during stalls. The one-second timer only exports a
  snapshot; it never drives frame presentation.

## Validation

Completed on 2026-08-10:

- `go test -race ./...` in `moqlivemock-svc`: passed, including the new native
  playback and LOC frame-marking tests.
- `.venv/bin/pytest -q` in moqlab: 177 passed, 1 gated integration test
  skipped.
- Every topology YAML under `configs/` validates, including the new
  `media_svc_metrics.yaml` smoke topology.
- `npm test -- --runInBand` in WARP Player: 26 suites passed, 1 skipped;
  386 tests passed, 7 skipped.
- `npm run typecheck`, `npm run lint`, and `npm run pretty`: passed. ESLint
  retained its existing package-module-type performance warning.
- `npm run build`: passed with the existing webpack bundle-size warnings.
- `node --check` for the Chrome runner and visualizer application: passed.
- Follow-up stable-panel validation: `go test -race ./...` passed, moqlab
  `.venv/bin/pytest -q` passed with 177 tests and 1 gated integration skip,
  the focused visualizer suite passed 12 tests, and `node --check
  visualizer/app.js` passed. The native playback suite now explicitly verifies
  changing E2E latency and buffer values against changing simulated state.
- Per-frame follow-up: the native image rebuilt successfully and an isolated
  Docker smoke ran against `media_svc_metrics.yaml`. Eight consecutive native
  samples remained `playing` on `video/s2` at exactly 1x; E2E latency held near
  315.24 ms, continuous-playhead buffer moved from 265.01 to 265.28 ms, and
  player bitrate tracked receive bitrate without backlog growth. The smoke run
  and its temporary run directory were removed.
- `python -m moqlab build media-images`: rebuilt publisher, Chrome subscriber,
  and native subscriber images successfully from the changed sources.
- Docker smoke using `configs/examples/media_svc_metrics.yaml`: passed. Native
  ran only `mlmsub`; native and headless Chrome both reported `playing`, active
  `video/s2`, fresh E2E/bitrate/buffer/quality metrics, and the selected-node
  API returned `status: ok` for both. The run was torn down and its temporary
  run directory removed.
- Containernet's dedicated Python environment passes `moqlab doctor`; the
  kernel and images are ready. An actual Containernet launch could not be
  completed from this session because host `sudo` requested the user's
  password. This remains the only unexecuted acceptance step.

An extra `compileall` check could not replace root-owned existing
`__pycache__` files. The full pytest run imported and exercised the changed
Python modules successfully, so this is workspace ownership cleanup rather
than a source failure.

## Known Limits And Future Work

- Native playback models encoded-unit correctness and timing, not codec cost.
- On a healthy fixed-rate stream, E2E latency and buffer can settle at constant
  values: the publisher, player, and one-second sampler are phase-aligned. The
  sample timestamp distinguishes a fresh stable value from a stale sample;
  no artificial jitter is added to measurements.
- The switching trigger is decode availability, not a bandwidth estimator.
- A future controller can change the target chain while retaining the same
  safe transition rules.
- Metrics are live and ephemeral; experiment archival remains separate work.
