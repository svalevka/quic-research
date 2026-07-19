"""
TCP vs QUIC comparison client.

Subcommands:
  handshake <tcp|quic> <host> <port> [--trials N]
  throughput <tcp|quic> <host> <port> [--sizes 1024,65536,...] [--trials N]
  hol <tcp|quic> <host> <port> [--streams N] [--bytes N] [--chunk N]
"""
import argparse
import asyncio
import os
import ssl
import struct
import time

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived

HEADER = struct.Struct("!HI")  # stream_id, length


def quic_config(cafile):
    config = QuicConfiguration(is_client=True)
    config.load_verify_locations(cafile=cafile)
    return config


def tcp_ssl_context(cafile):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cafile)
    return ctx


# ---------------------------------------------------------------------------
# handshake timing
# ---------------------------------------------------------------------------

async def tcp_handshake_once(host, port, cafile):
    ctx = tcp_ssl_context(cafile)
    t0 = time.perf_counter()
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx, server_hostname=host)
    dt = time.perf_counter() - t0
    writer.close()
    await writer.wait_closed()
    return dt


async def quic_handshake_once(host, port, cafile):
    t0 = time.perf_counter()
    async with connect(host, port, configuration=quic_config(cafile)) as protocol:
        dt = time.perf_counter() - t0
    return dt


async def cmd_handshake(args):
    times = []
    for i in range(args.trials):
        if args.proto == "tcp":
            dt = await tcp_handshake_once(args.host, args.port, args.cafile)
        else:
            dt = await quic_handshake_once(args.host, args.port, args.cafile)
        times.append(dt)
        print(f"  trial {i+1}: {dt*1000:.2f} ms")
    times.sort()
    n = len(times)
    print(f"\n[{args.proto}] handshake over {n} trials:")
    print(f"  min={times[0]*1000:.2f}ms  median={times[n//2]*1000:.2f}ms  max={times[-1]*1000:.2f}ms")


# ---------------------------------------------------------------------------
# throughput (single stream, echo, varying sizes)
# ---------------------------------------------------------------------------

async def tcp_echo_roundtrip(reader, writer, size: int):
    data = os.urandom(size)
    header = HEADER.pack(0, size)
    t0 = time.perf_counter()
    writer.write(header + data)
    await writer.drain()
    resp_header = await reader.readexactly(HEADER.size)
    _, resp_len = HEADER.unpack(resp_header)
    got = 0
    while got < resp_len:
        chunk = await reader.read(resp_len - got)
        got += len(chunk)
    return time.perf_counter() - t0


async def cmd_throughput_tcp(args, sizes):
    ctx = tcp_ssl_context(args.cafile)
    for size in sizes:
        samples = []
        for _ in range(args.trials):
            reader, writer = await asyncio.open_connection(
                args.host, args.port, ssl=ctx, server_hostname=args.host
            )
            dt = await tcp_echo_roundtrip(reader, writer, size)
            writer.close()
            await writer.wait_closed()
            samples.append(dt)
        report_size(size, samples)


class ThroughputQuicProtocol(QuicConnectionProtocol):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pending = {}

    async def echo_once(self, size: int):
        data = os.urandom(size)
        stream_id = self._quic.get_next_available_stream_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[stream_id] = {"received": 0, "expected": size, "fut": fut}
        t0 = time.perf_counter()
        self._quic.send_stream_data(stream_id, data, end_stream=True)
        self.transmit()
        await fut
        return time.perf_counter() - t0

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            entry = self._pending.get(event.stream_id)
            if entry is None:
                return
            entry["received"] += len(event.data)
            if entry["received"] >= entry["expected"] and not entry["fut"].done():
                entry["fut"].set_result(None)


async def cmd_throughput_quic(args, sizes):
    async with connect(
        args.host, args.port, configuration=quic_config(args.cafile),
        create_protocol=ThroughputQuicProtocol,
    ) as protocol:
        for size in sizes:
            samples = []
            for _ in range(args.trials):
                dt = await protocol.echo_once(size)
                samples.append(dt)
            report_size(size, samples)


def report_size(size, samples):
    samples.sort()
    n = len(samples)
    med = samples[n // 2]
    mbps = (2 * size * 8 / med) / 1e6  # round trip = 2x size, over median rtt
    human = f"{size/1024:.0f}KB" if size < 1024 * 1024 else f"{size/1024/1024:.1f}MB"
    print(f"  {human:>8}  median={med*1000:8.2f}ms  min={samples[0]*1000:8.2f}ms  ~{mbps:8.2f} Mbps")


async def cmd_throughput(args):
    sizes = [int(s) for s in args.sizes.split(",")]
    print(f"[{args.proto}] throughput over {args.trials} trials/size:")
    if args.proto == "tcp":
        await cmd_throughput_tcp(args, sizes)
    else:
        await cmd_throughput_quic(args, sizes)


# ---------------------------------------------------------------------------
# head-of-line blocking demo: N concurrent streams, one connection
# ---------------------------------------------------------------------------

async def hol_tcp(args):
    ctx = tcp_ssl_context(args.cafile)
    reader, writer = await asyncio.open_connection(
        args.host, args.port, ssl=ctx, server_hostname=args.host
    )
    n_streams = args.streams
    total = args.bytes
    chunk = args.chunk

    received = {i: 0 for i in range(n_streams)}
    complete_ts = {}
    start = time.perf_counter()
    done = asyncio.Event()

    async def sender():
        remaining = {i: total for i in range(n_streams)}
        payload = os.urandom(chunk)
        while any(v > 0 for v in remaining.values()):
            for sid in range(n_streams):
                if remaining[sid] <= 0:
                    continue
                n = min(chunk, remaining[sid])
                writer.write(HEADER.pack(sid, n) + payload[:n])
                remaining[sid] -= n
            await writer.drain()

    async def receiver():
        while len(complete_ts) < n_streams:
            header = await reader.readexactly(HEADER.size)
            sid, length = HEADER.unpack(header)
            got = 0
            while got < length:
                chunk_data = await reader.read(length - got)
                got += len(chunk_data)
            received[sid] += length
            if received[sid] >= total and sid not in complete_ts:
                complete_ts[sid] = time.perf_counter() - start
        done.set()

    await asyncio.gather(sender(), receiver())
    writer.close()
    await writer.wait_closed()
    return complete_ts


async def hol_quic(args):
    n_streams = args.streams
    total = args.bytes
    chunk = args.chunk

    class HolProtocol(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.received = {}
            self.complete_ts = {}
            self.start = None
            self.done = asyncio.Event()
            self.stream_ids = []

        def begin(self):
            self.start = time.perf_counter()
            remaining = {}
            payload = os.urandom(chunk)
            # Fetch each stream ID immediately followed by its first send: aioquic
            # only advances get_next_available_stream_id() once a stream has
            # actually been used, so batching the lookups upfront returns the
            # same ID repeatedly and silently drops all but one stream.
            for _ in range(n_streams):
                sid = self._quic.get_next_available_stream_id()
                self.stream_ids.append(sid)
                self.received[sid] = 0
                remaining[sid] = total
                n = min(chunk, remaining[sid])
                is_last = remaining[sid] - n <= 0
                self._quic.send_stream_data(sid, payload[:n], end_stream=is_last)
                remaining[sid] -= n
            while any(v > 0 for v in remaining.values()):
                for sid in self.stream_ids:
                    if remaining[sid] <= 0:
                        continue
                    n = min(chunk, remaining[sid])
                    is_last = remaining[sid] - n <= 0
                    self._quic.send_stream_data(sid, payload[:n], end_stream=is_last)
                    remaining[sid] -= n
            self.transmit()

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                sid = event.stream_id
                self.received[sid] += len(event.data)
                if self.received[sid] >= total and sid not in self.complete_ts:
                    self.complete_ts[sid] = time.perf_counter() - self.start
                    if len(self.complete_ts) == n_streams:
                        self.done.set()

    async with connect(
        args.host, args.port, configuration=quic_config(args.cafile),
        create_protocol=HolProtocol,
    ) as protocol:
        protocol.begin()
        await protocol.done.wait()
        return protocol.complete_ts


async def cmd_hol(args):
    print(f"[{args.proto}] HOL demo: {args.streams} streams x {args.bytes} bytes "
          f"(chunk={args.chunk}) over one connection")
    if args.proto == "tcp":
        complete_ts = await hol_tcp(args)
    else:
        complete_ts = await hol_quic(args)
    for sid in sorted(complete_ts):
        print(f"  stream {sid}: completed at {complete_ts[sid]*1000:8.2f} ms")
    vals = list(complete_ts.values())
    print(f"  spread (max-min): {(max(vals)-min(vals))*1000:.2f} ms")


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in [("handshake", cmd_handshake), ("throughput", cmd_throughput), ("hol", cmd_hol)]:
        sp = sub.add_parser(name)
        sp.add_argument("proto", choices=["tcp", "quic"])
        sp.add_argument("host")
        sp.add_argument("port", type=int)
        sp.add_argument("--trials", type=int, default=5)
        sp.add_argument("--sizes", default="1024,65536,262144,1048576,8388608")
        sp.add_argument("--streams", type=int, default=4)
        sp.add_argument("--bytes", type=int, default=2 * 1024 * 1024)
        sp.add_argument("--chunk", type=int, default=16 * 1024)
        sp.add_argument("--cafile", default=os.path.join(os.path.dirname(__file__), "cert.pem"))
        sp.set_defaults(func=fn)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
