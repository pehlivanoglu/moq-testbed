from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import time
from pathlib import Path

_UDP_HEADER = struct.Struct("!IQ")
_PAYLOAD = bytes(65536)
_LOG_CONTEXT: dict[str, object] = {}


def _log(event: str, **fields: object) -> None:
    print(
        json.dumps(
            {
                "ts": time.time(),
                **_LOG_CONTEXT,
                "event": event,
                "level": "INFO",
                **fields,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def _load_plan(path: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("traffic plan must be a JSON object")
    return value


async def _receive_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        header = json.loads((await reader.readline()).decode())
        flow_id = header["flow_id"]
        connection = header["connection"]
        total = 0
        started = time.monotonic()
        _log("tcp_receive_start", flow_id=flow_id, connection=connection, peer=peer)
        while chunk := await reader.read(65536):
            total += len(chunk)
        elapsed = time.monotonic() - started
        _log(
            "tcp_receive_end",
            flow_id=flow_id,
            connection=connection,
            bytes=total,
            elapsed_s=elapsed,
        )
    except Exception as error:
        _log("tcp_receive_error", peer=peer, error=str(error))
    finally:
        writer.close()
        await writer.wait_closed()


class _UdpReceiver(asyncio.DatagramProtocol):
    def __init__(self, flow_ids: list[str]) -> None:
        self.flow_ids = flow_ids
        self.counts: dict[int, tuple[int, int]] = {}
        self.reported: dict[int, tuple[int, int]] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        asyncio.get_running_loop().call_later(1, self._report)

    def datagram_received(self, data: bytes, _address) -> None:
        if len(data) < _UDP_HEADER.size:
            return
        flow_index, _sequence = _UDP_HEADER.unpack_from(data)
        packets, size = self.counts.get(flow_index, (0, 0))
        self.counts[flow_index] = (packets + 1, size + len(data))

    def _report(self) -> None:
        for flow_index, (packets, size) in sorted(self.counts.items()):
            if self.reported.get(flow_index) == (packets, size):
                continue
            self.reported[flow_index] = (packets, size)
            flow_id = (
                self.flow_ids[flow_index]
                if flow_index < len(self.flow_ids)
                else f"unknown-{flow_index}"
            )
            _log("udp_receive_total", flow_id=flow_id, packets=packets, bytes=size)
        if self.transport is not None:
            asyncio.get_running_loop().call_later(1, self._report)


async def _run_receiver(plan: dict[str, object]) -> None:
    tcp_port = int(plan["tcp_port"])
    udp_port = int(plan["udp_port"])
    flows = plan["flows"]
    assert isinstance(flows, list)
    flow_ids = [str(flow["id"]) for flow in flows]
    tcp_server = await asyncio.start_server(_receive_tcp, "0.0.0.0", tcp_port)
    loop = asyncio.get_running_loop()
    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpReceiver(flow_ids), local_addr=("0.0.0.0", udp_port)
    )
    _log("receiver_ready", tcp_port=tcp_port, udp_port=udp_port)
    try:
        async with tcp_server:
            await tcp_server.serve_forever()
    finally:
        udp_transport.close()


async def _open_tcp(
    plan: dict[str, object], flow: dict[str, object], connection: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    routes = plan["routes"]
    assert isinstance(routes, dict)
    route = routes[str(flow["route"])]
    assert isinstance(route, dict)
    reader, writer = await asyncio.open_connection(
        str(route["receiver_ip"]),
        int(plan["tcp_port"]),
        local_addr=(str(route["sender_ip"]), 0),
    )
    writer.write(
        json.dumps(
            {"flow_id": flow["id"], "connection": connection},
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    await writer.drain()
    return reader, writer


async def _bulk_connection(
    plan: dict[str, object], flow: dict[str, object], connection: int, deadline: float
) -> None:
    _reader, writer = await _open_tcp(plan, flow, connection)
    chunk = _PAYLOAD[: int(flow["chunk_bytes"])]
    total = 0
    try:
        while time.monotonic() < deadline:
            writer.write(chunk)
            await writer.drain()
            total += len(chunk)
    finally:
        writer.close()
        await writer.wait_closed()
    _log("bulk_end", flow_id=flow["id"], connection=connection, bytes=total)


async def _send_exact(writer: asyncio.StreamWriter, size: int) -> None:
    remaining = size
    while remaining:
        chunk = _PAYLOAD[: min(remaining, len(_PAYLOAD))]
        writer.write(chunk)
        await writer.drain()
        remaining -= len(chunk)


async def _segmented_client(
    plan: dict[str, object], flow: dict[str, object], connection: int, deadline: float
) -> None:
    _reader, writer = await _open_tcp(plan, flow, connection)
    duration_ms = int(flow["segment_duration_ms"])
    rates = [float(value) for value in flow["representation_sequence_mbps"]]
    interval = duration_ms / 1000
    segment = 0
    total = 0
    next_at = time.monotonic()
    try:
        while next_at < deadline:
            await asyncio.sleep(max(0, next_at - time.monotonic()))
            size = max(1, round(rates[segment % len(rates)] * duration_ms * 125))
            started = time.monotonic()
            await _send_exact(writer, size)
            total += size
            _log(
                "segment_sent",
                flow_id=flow["id"],
                connection=connection,
                segment=segment,
                bytes=size,
                elapsed_s=time.monotonic() - started,
            )
            segment += 1
            next_at += interval
    finally:
        writer.close()
        await writer.wait_closed()
    _log("segmented_end", flow_id=flow["id"], connection=connection, bytes=total)


async def _run_cbr(
    plan: dict[str, object], flow: dict[str, object], flow_index: int, deadline: float
) -> None:
    routes = plan["routes"]
    assert isinstance(routes, dict)
    route = routes[str(flow["route"])]
    assert isinstance(route, dict)
    packet_size = int(flow["packet_size_bytes"])
    interval = packet_size * 8 / (float(flow["rate_mbps"]) * 1_000_000)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((str(route["sender_ip"]), 0))
    sock.connect((str(route["receiver_ip"]), int(plan["udp_port"])))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    sequence = 0
    late = 0
    next_at = time.monotonic()
    padding = bytes(packet_size - _UDP_HEADER.size)
    try:
        while next_at < deadline:
            now = time.monotonic()
            if now - next_at > interval:
                missed = int((now - next_at) // interval)
                late += missed
                next_at += missed * interval
            if now < next_at:
                await asyncio.sleep(next_at - now)
            header = _UDP_HEADER.pack(flow_index, sequence)
            await loop.sock_sendall(sock, header + padding)
            sequence += 1
            next_at += interval
    finally:
        sock.close()
    _log("cbr_end", flow_id=flow["id"], packets=sequence, late_packets=late)


async def _run_flow(
    plan: dict[str, object], flow: dict[str, object], flow_index: int, epoch: float
) -> None:
    await asyncio.sleep(max(0, epoch + float(flow["start_s"]) - time.monotonic()))
    deadline = time.monotonic() + float(flow["duration_s"])
    _log("flow_start", flow_id=flow["id"], kind=flow["kind"], route=flow["route"])
    if flow["kind"] == "bulk":
        await asyncio.gather(
            *(
                _bulk_connection(plan, flow, connection, deadline)
                for connection in range(int(flow["connections"]))
            )
        )
    elif flow["kind"] == "segmented":
        await asyncio.gather(
            *(
                _segmented_client(plan, flow, connection, deadline)
                for connection in range(int(flow["clients"]))
            )
        )
    else:
        await _run_cbr(plan, flow, flow_index, deadline)
    _log("flow_end", flow_id=flow["id"])


async def _run_sender(plan: dict[str, object]) -> None:
    flows = plan["flows"]
    assert isinstance(flows, list)
    epoch = time.monotonic()
    _log("sender_start", flows=len(flows))
    await asyncio.gather(
        *(_run_flow(plan, flow, index, epoch) for index, flow in enumerate(flows))
    )
    _log("sender_end")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("sender", "receiver"))
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    plan = _load_plan(args.plan)
    _LOG_CONTEXT.update(
        run_id=plan.get("run_id", "standalone"),
        node=plan[f"{args.role}_id"],
        node_type=f"traffic_{args.role}",
    )
    asyncio.run(_run_sender(plan) if args.role == "sender" else _run_receiver(plan))


if __name__ == "__main__":
    main()
