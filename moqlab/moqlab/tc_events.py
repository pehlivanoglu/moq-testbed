from __future__ import annotations

import argparse
import json
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path


_HEADER = struct.Struct("=IHHII")
_TCMSG = struct.Struct("=BBHiIII")
_ATTR = struct.Struct("=HH")
_NEW_QDISC, _DEL_QDISC, _GET_QDISC = 36, 37, 38
_NEW_CLASS, _DEL_CLASS, _GET_CLASS = 40, 41, 42


def _attributes(data: bytes) -> dict[int, bytes]:
    attributes = {}
    offset = 0
    while offset + _ATTR.size <= len(data):
        length, kind = _ATTR.unpack_from(data, offset)
        if length < _ATTR.size or offset + length > len(data):
            raise ValueError("truncated tc netlink attribute")
        attributes[kind & 0x3FFF] = data[offset + _ATTR.size:offset + length]
        offset += (length + 3) & ~3
    return attributes


def _rates(kind: str, data: bytes) -> dict[str, int]:
    # shortcut: HTB/TBF/netem/CAKE rate layouts; extend when another rate shaper is used.
    # Linux UAPI: include RATE64 overrides so high-capacity links do not truncate.
    options = _attributes(data[24:] if kind == "netem" else data)
    if kind in ("htb", "tbf") and 1 in options:
        parameters = options[1]
        rate = struct.unpack_from("=I", parameters, 8)[0]
        ceiling = struct.unpack_from("=I", parameters, 20)[0]
        rate64, ceiling64 = (6, 7) if kind == "htb" else (4, 5)
        return {
            "rate_bps": struct.unpack("=Q", options[rate64])[0] * 8 if rate64 in options else rate * 8,
            "ceil_bps" if kind == "htb" else "peakrate_bps":
                struct.unpack("=Q", options[ceiling64])[0] * 8 if ceiling64 in options else ceiling * 8,
        }
    if kind == "netem" and 6 in options:
        return {"rate_bps": (struct.unpack("=Q", options[8])[0] if 8 in options
                             else struct.unpack_from("=I", options[6])[0]) * 8}
    if kind == "cake" and 2 in options:
        return {"rate_bps": struct.unpack("=Q", options[2])[0] * 8}
    return {}


def decode_notifications(data: bytes) -> list[dict]:
    notifications = []
    offset = 0
    while offset + _HEADER.size <= len(data):
        length, message_type, flags, sequence, _pid = _HEADER.unpack_from(data, offset)
        if length < _HEADER.size or offset + length > len(data):
            raise ValueError("truncated tc netlink message")
        payload = data[offset + _HEADER.size:offset + length]
        if message_type == 4:  # NLMSG_OVERRUN
            raise RuntimeError("tc netlink overrun; some changes are missing")
        if message_type == 2:
            error = struct.unpack_from("=i", payload)[0]
            if error:
                raise OSError(-error, "tc netlink dump failed")
        if message_type == 3:
            if flags & 0x10:  # NLM_F_DUMP_INTR: a partial baseline is not trustworthy.
                raise RuntimeError("tc baseline dump was interrupted")
            notifications.append({"done": sequence})
        elif message_type in (_NEW_QDISC, _DEL_QDISC, _NEW_CLASS, _DEL_CLASS):
            _family, _pad, _pad2, ifindex, handle, parent, _info = _TCMSG.unpack_from(payload)
            attributes = _attributes(payload[_TCMSG.size:])
            kind = attributes.get(1, b"unknown\0").rstrip(b"\0").decode()
            # Only bandwidth-capable kinds have rate options; FIFO/AQM options are opaque.
            rates = _rates(kind, attributes[2]) if kind in ("htb", "tbf", "netem", "cake") and 2 in attributes else {}
            notifications.append({
                "ifindex": ifindex, "handle": handle, "parent": parent, "kind": kind,
                "object": "class" if message_type in (_NEW_CLASS, _DEL_CLASS) else "qdisc",
                "deleted": message_type in (_DEL_QDISC, _DEL_CLASS),
                "rates": rates, "sequence": sequence,
                "multipart": bool(flags & 2),
            })
        offset += (length + 3) & ~3
    if offset != len(data):
        raise ValueError("incomplete tc netlink header")
    return notifications


class RateChanges:
    def __init__(self) -> None:
        self.rates: dict[tuple, dict[str, int]] = {}

    def update(self, message: dict, initial: bool = False) -> list[dict]:
        key = (message["ifindex"], message["object"], message["handle"])
        removed = [key]
        if message["deleted"] and message["object"] == "qdisc":
            removed += [other for other in self.rates if other != key and other[0] == key[0]
                        and other[2] >> 16 == key[2] >> 16]
        changes = []
        for current in removed:
            before = self.rates.get(current, {})
            after = {} if message["deleted"] else {field: value for field, value in message["rates"].items() if value}
            if before != after:
                changes.append({
                    "event": "bandwidth_initial" if initial else "bandwidth_change",
                    "ifindex": current[0], "object": current[1],
                    "handle": f"{current[2] >> 16:x}:{current[2] & 0xFFFF:x}",
                    "kind": message["kind"], "before": before, "after": after,
                })
            if after:
                self.rates[current] = after
            else:
                self.rates.pop(current, None)
        return changes


def collect(node: str, output: Path, ready: Path) -> None:
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with output.open("x", buffering=1) as archive, socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as listener:
        def log(event: dict) -> None:
            archive.write(json.dumps({"timestamp_unix_ns": time.time_ns(),
                                      "timestamp_mono_ns": time.monotonic_ns(),
                                      "node": node, **event}) + "\n")

        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            listener.bind((0, 8))  # RTMGRP_TC: all qdisc/class changes in this namespace.
            listener.settimeout(0.2)
            state = RateChanges()
            interfaces = dict(socket.if_nameindex())
            pending = [_GET_QDISC, _GET_CLASS]
            sequence = 0

            def dump() -> None:
                nonlocal sequence
                sequence += 1
                payload = _TCMSG.pack(socket.AF_UNSPEC, 0, 0, 0, 0, 0, 0)
                listener.sendto(_HEADER.pack(_HEADER.size + len(payload), pending.pop(0), 0x301, sequence, 0) + payload, (0, 0))

            dump()
            while True:
                try:
                    data, _ancillary, flags, _address = listener.recvmsg(1 << 20)
                except socket.timeout:
                    if stopping:
                        break
                    continue
                if flags & socket.MSG_TRUNC:
                    raise RuntimeError("tc event buffer truncated; some changes are missing")
                observed = {"timestamp_unix_ns": time.time_ns(), "timestamp_mono_ns": time.monotonic_ns()}
                for message in decode_notifications(data):
                    if "done" in message:
                        if message["done"] == sequence:
                            if pending:
                                dump()
                            else:
                                ready.touch(exist_ok=False)
                        continue
                    initial = message["multipart"] and message["sequence"] in (1, 2)
                    for event in state.update(message, initial):
                        ifindex = event.pop("ifindex")
                        try:
                            interfaces[ifindex] = socket.if_indextoname(ifindex)
                        except OSError:
                            pass  # Deleted interfaces may already have disappeared from the namespace.
                        event["interface"] = interfaces.get(ifindex, f"ifindex-{ifindex}")
                        log({**observed, **event})
        except Exception as error:
            log({"event": "monitor_error", "error": str(error)})
            raise


class TcEventMonitors:
    def __init__(self, directory: Path, nodes: list[str]) -> None:
        self.directory = directory
        self.nodes = nodes
        self.processes: list[tuple[str, subprocess.Popen]] = []

    def start(self) -> None:
        from moqlab.exceptions import OrchestratorError

        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Pid}}", *[f"mn.{node}" for node in self.nodes]],
                capture_output=True, text=True, check=True,
            )
            pids = [int(value) for value in result.stdout.splitlines()]
            if len(pids) != len(self.nodes) or any(pid <= 0 for pid in pids):
                raise RuntimeError("could not resolve all node network namespaces")
            for node, pid in zip(self.nodes, pids):
                with (self.directory / f"{node}.log").open("x") as errors:
                    process = subprocess.Popen([
                        "nsenter", "-t", str(pid), "-n", sys.executable, str(Path(__file__).resolve()),
                        "--node", node, "--output", str((self.directory / f"{node}.jsonl").resolve()),
                        "--ready", str((self.directory / f"{node}.ready").resolve()),
                    ], stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
                self.processes.append((node, process))
            deadline = time.monotonic() + 5
            while any(not (self.directory / f"{node}.ready").exists() for node in self.nodes):
                for node, process in self.processes:
                    if process.poll() is not None:
                        raise RuntimeError(f"{node}: tc event monitor failed; see {self.directory / (node + '.log')}")
                if time.monotonic() >= deadline:
                    raise RuntimeError("tc event monitors did not become ready within 5 seconds")
                time.sleep(0.01)
        except Exception as error:
            try:
                self.stop()
            except OrchestratorError:
                pass
            raise OrchestratorError(f"cannot start bandwidth event recording: {error}") from error

    def stop(self) -> None:
        from moqlab.exceptions import OrchestratorError

        failures = []
        ended_early = set()
        for node, process in self.processes:
            if process.poll() is not None:
                ended_early.add(node)
            else:
                process.terminate()
        for node, process in self.processes:
            try:
                status = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                status = -9
            if status or node in ended_early:
                failures.append(node)
                with (self.directory / f"{node}.jsonl").open("a") as archive:
                    archive.write(json.dumps({
                        "event": "monitor_error", "node": node,
                        "timestamp_unix_ns": time.time_ns(), "timestamp_mono_ns": time.monotonic_ns(),
                        "error": f"monitor exited {'before teardown' if node in ended_early else 'during shutdown'} "
                                 f"with status {status}; bandwidth history may be incomplete",
                    }) + "\n")
        self.processes.clear()
        if failures:
            raise OrchestratorError(f"bandwidth event recording incomplete for {', '.join(failures)}; see {self.directory}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    collect(args.node, args.output, args.ready)
