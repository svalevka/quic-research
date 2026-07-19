import asyncio
import ssl
import struct
import sys

HEADER = struct.Struct("!HI")  # stream_id, length


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    print(f"[tcp] connection from {peer}")
    try:
        while True:
            header = await reader.readexactly(HEADER.size)
            stream_id, length = HEADER.unpack(header)
            payload = await reader.readexactly(length)
            writer.write(header + payload)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        print(f"[tcp] connection closed {peer}")
        writer.close()


async def main(port: int, cert: str, key: str):
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert, key)

    server = await asyncio.start_server(handle, "0.0.0.0", port, ssl=ssl_ctx)
    print(f"[tcp+tls] echo server listening on :{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
    cert = sys.argv[2] if len(sys.argv) > 2 else "cert.pem"
    key = sys.argv[3] if len(sys.argv) > 3 else "key.pem"
    asyncio.run(main(port, cert, key))
