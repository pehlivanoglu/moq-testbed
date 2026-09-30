# Wei et al. shared-bottleneck detector

`sbd.algorithm: Wei2020ECN` selects the sender-side shared-bottleneck
detection portion of Wei et al., *Shared Bottleneck-Based Congestion Control
and Packet Scheduling for Multipath TCP* (IEEE/ACM ToN, 2020). It does not
implement SB-CC window adjustment, congestion degree, coupled congestion
control, SB-FPS, scheduling, or bitrate adaptation.

## Transport mapping

| Original behavior | QUIC/MoQ adaptation | Why |
|---|---|---|
| MPTCP sender | MoQ edge relay | The relay sends media to every downstream client. |
| MPTCP subflow | Active downstream QUIC connection | The independently paced connections are the paths being compared. Relay peers and publisher-only sessions are excluded. |
| ECE-marked ACK triggers detection | An AppData ACK_ECN whose cumulative CE count exceeds the connection's previous high-water mark | QUIC reports ECN through cumulative ACK_ECN counters. Reordered lower counter values are ignored. One received ACK_ECN is one event even if its CE delta exceeds one. |
| TCP loss/RTO is supporting evidence | mvfst `packetLossDetected`, after QUIC officially declares an AppData packet lost | QUIC PTO only sends probes and is not a loss declaration. PTO is never a congestion event. A later spurious-loss report does not erase the historical declaration. |
| Subflow congestion window measured in packets | `floor(cwnd_bytes / mss)`, with each past and future half-window equal to `floor(cwnd_packets / 2)` | mvfst exposes congestion window and maximum datagram size in bytes. |
| Packet-position observation window | Dense ordinal assigned to each ACK-eliciting AppData packet written by the relay | Raw QUIC packet numbers include gaps and non-ACK-eliciting packets. ACK-eliciting AppData packets are congestion-controlled and are the closest equivalent to TCP data packets. |
| Future half-window | Remains open until every packet through its upper ordinal is ACKed or officially declared lost | This preserves a packet/feedback window without introducing a fixed-time approximation. A connection that stops progressing keeps the judgment pending until it progresses or leaves the active set. |
| One MPTCP connection's current subflows | Current eligible downstream client connections at the relay | QUIC connections can independently join and leave. Leaving dissolves a group containing that connection; an outside connection leaving is removed from a pending check. |

Packet ordinals and feedback progress start when a connection first becomes an
eligible downstream subscriber. Cumulative ECN high-water marks are retained
across eligibility changes so pre-existing CE counts cannot become new events.

## State machine

All eligible connections begin in `MONITORING`. Only a CE-counter increase can
start a judgment. Stage 1 places the trigger and every monitoring connection
with CE or declared-loss evidence in the same packet window into a preliminary
set and moves them to `JUDGEMENT`. A later CE from a member starts Stage 2.
Every other member must have CE or loss evidence, and every outside connection
still in `MONITORING` must have none. Success moves the set to
`FINAL_JUDGEMENT`; failure returns its members to `MONITORING`. Confirmed sets
run the same verification again on later member CE events and dissolve on
failure.

The paper does not specify arbitration between overlapping in-flight future
windows. This implementation permits one pending preliminary observation per
monitoring trigger and one pending verification per set. If another observation
changes a trigger's state first, the stale observation is discarded. This
preserves the three paper states and prevents one connection from belonging to
multiple sets.

## Output

`GET /sbd-metrics` reports current connection states, preliminary sets,
confirmed groups, independent connections, ECN counters, congestion windows,
and packet progress. The configured JSONL output stores snapshots. Its
`.events.jsonl` companion records CE/loss events, window bounds, candidate
members, verification results, group creation, confirmation, and invalidation.
The compatibility `delay_source` setting is ignored by this packet-domain
detector.
