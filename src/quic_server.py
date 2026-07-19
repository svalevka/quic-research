import sys
import asyncio

from aioquic.asyncio import serve, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived, HandshakeCompleted


class ServerProtocol(QuicConnectionProtocol):
    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            print("[quic] handshake completed with client")
        elif isinstance(event, StreamDataReceived):
            self._quic.send_stream_data(
                event.stream_id, event.data, end_stream=event.end_stream
            )
            self.transmit()


async def main(port: int, cert: str, key: str):
    config = QuicConfiguration(is_client=False)
    config.load_cert_chain(cert, key)

    await serve(
        "0.0.0.0",
        port,
        configuration=config,
        create_protocol=ServerProtocol,
    )
    print(f"[quic] echo server listening on udp/:{port}")
    await asyncio.Future()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4433
    cert = sys.argv[2] if len(sys.argv) > 2 else "cert.pem"
    key = sys.argv[3] if len(sys.argv) > 3 else "key.pem"
    asyncio.run(main(port, cert, key))
