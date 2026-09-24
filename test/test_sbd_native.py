#!/usr/bin/env python3
"""Local native-client SBD interoperability check (no root or Docker required)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen


def run(relay_binary: Path, publisher: Path, subscriber: Path, mode: str,
        output: Path, unsupported: bool = False) -> None:
    root = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "snapshots.jsonl"
    if archive.exists():
        raise ValueError(f"refusing to mix runs in {archive}")
    ports = set()
    while len(ports) < 3:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                try:
                    udp.bind(("127.0.0.1", port))
                except OSError:
                    continue
                ports.add(port)
    pub_port, relay_port, admin_port = sorted(ports)
    config = f"""relay_id: native-sbd-validation
edge: true
sbd:
  enabled: true
  algorithm: PracticalPassiveAndRFC
  delay_source: {mode}
  output_file: {json.dumps(str(archive))}
listeners:
  - name: main
    udp:
      socket: {{address: "127.0.0.1", port: {relay_port}}}
    tls: {{insecure: true}}
    endpoint: /moq-relay
services:
  default:
    match:
      - authority: {{any: true}}
        path: {{prefix: "/"}}
    cache: {{enabled: true, max_tracks: 100, max_groups_per_track: 3}}
    upstream:
      url: moqt://127.0.0.1:{pub_port}/moq
      tls: {{insecure: true}}
admin: {{port: {admin_port}, address: "127.0.0.1", plaintext: true}}
"""
    (output / "relay.yaml").write_text(config)
    processes = []
    logs = []

    def launch(name: str, args: list[str]) -> None:
        log = (output / f"{name}.log").open("w")
        logs.append(log)
        processes.append(subprocess.Popen(args, cwd=output, stdout=log, stderr=subprocess.STDOUT))

    def read(endpoint: str) -> dict:
        with urlopen(f"http://127.0.0.1:{admin_port}/{endpoint}", timeout=2) as response:
            return json.load(response)

    try:
        launch("publisher", [str(publisher), "-addr", f"127.0.0.1:{pub_port}",
              "-asset", str(root.parent / "moqlivemock-svc/assets/testsvc"),
              "-cert", str(root / "test/test_cert.pem"), "-key", str(root / "test/test_key.pem")])
        time.sleep(0.5)
        launch("relay", [str(relay_binary), "--config", str(output / "relay.yaml")])
        deadline = time.monotonic() + 10
        while True:
            try:
                read("sbd-metrics")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"relay failed to start; see {output}/relay.log")
                time.sleep(0.1)
        # Separate LOC and CMSF catalogs avoid the existing subscribe-only catalog
        # replay race for simultaneous subscribers; both carry the same video asset.
        for i, (namespace, track) in enumerate((("msf/clear", "video/s2"), ("cmsf/clear", "video"))):
            launch(f"subscriber-{i}", [str(subscriber), "-addr", f"127.0.0.1:{relay_port}",
                   "-namespace", namespace, "-videoname", track, "-subscribe-dependencies",
                   "-catalog-mode", "subscribe", "-draft", "16", "-duration", "45",
                   "-qlog", str(output / f"subscriber-{i}.qlog")])
        deadline = time.monotonic() + 42
        while time.monotonic() < deadline:
            time.sleep(0.35)
            data = read("sbd-metrics")
            assert data["algorithm"] == "PracticalPassiveAndRFC"
            clients = data["clients"]
            if unsupported:
                passed = len(clients) == 2 and all(c["status"] == "waiting_receive_timestamps" for c in clients)
            else:
                if any(c["status"].startswith("stopped_") for c in clients):
                    raise AssertionError(f"measurement stopped: {clients}")
                passed = len(clients) == 2 and all(c["status"] == "ready" and c["full_history"] for c in clients)
            if passed:
                if mode == "owd" and not unsupported:
                    assert data["receive_timestamp_basis"] == "linux_clock_monotonic"
                    assert data["feedback_grace_ms"] == 0
                    assert data["interval_completion"] == "receive_timestamp_watermark"
                    assert all(client["decision_valid"] for client in clients)
                    assert len({c["interval_end_mono_us"] for c in clients}) == 1
                    for client in clients:
                        assert client["interval_end_mono_us"] - client["interval_start_mono_us"] == 350_000
                        assert 0 <= client["interval_delay_min_us"] <= client["interval_delay_mean_us"]
                        assert client["interval_delay_mean_us"] <= client["interval_delay_max_us"]
                (output / "final.json").write_text(json.dumps(data, indent=2))
                (output / "network.json").write_text(json.dumps(read("network-metrics"), indent=2))
                print(f"PASS: {mode}, {'unsupported feedback waiting' if unsupported else 'two clients reached 100 intervals'}; {output}")
                break
        else:
            raise AssertionError(f"timed out waiting for measurement state: {data}")
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", type=Path, default=Path(__file__).resolve().parents[1] / "build/moqx")
    parser.add_argument("--publisher", type=Path, required=True)
    parser.add_argument("--subscriber", type=Path, required=True)
    parser.add_argument("--mode", choices=("owd", "rtt"), default="owd")
    parser.add_argument("--expect-unsupported", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix=f"sbd-{args.mode}-"))
    run(args.relay.resolve(), args.publisher.resolve(), args.subscriber.resolve(),
        args.mode, output.resolve(), args.expect_unsupported)
