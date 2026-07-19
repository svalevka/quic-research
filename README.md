# QUIC vs TCP+TLS, over a real internet path

This repository runs a real network test — not a simulation — comparing QUIC
against classic TCP+TLS. The client is a laptop on a home network in London;
the server is a Linux (Ubuntu) Hetzner Cloud VPS in Helsinki, Finland,
referred to throughout this repo's scripts and docs as `cherry`. Traffic
crosses the real internet, with a real round trip time of roughly 45–55ms.
No artificial delay is added anywhere. Packet loss, where used, is injected
for real with `tc netem` on `cherry`'s network interface — a Linux-only
tool, which is also why loss injection happens server-side rather than on
the (macOS) client; see [How the test scripts work](#how-the-test-scripts-work).

The goal is to make two specific, falsifiable claims concrete:

1. **QUIC's handshake completes in fewer round trips than TCP+TLS's**, because
   QUIC folds transport and cryptographic negotiation into the same exchange
   instead of running them as two separate sequential protocols.
2. **A single lost packet stalls only the QUIC stream it belonged to**, while
   the same loss on TCP+TLS stalls *every* stream sharing that connection,
   because TCP has no concept of "stream" at the transport layer — it's one
   ordered byte pipe, however many logical requests are multiplexed onto it.

Everything below is written so that someone who has never encountered QUIC
before can follow the argument from first principles. If you already know
what a nonce is and why TCP is head-of-line blocked, skip to
[What this repository actually measures](#what-this-repository-actually-measures).

## Contents

- [Why a handshake exists at all](#why-a-handshake-exists-at-all)
- [Two separate problems with HTTPS over TCP](#two-separate-problems-with-https-over-tcp)
- [What "one ordered line" means, at the packet level](#what-one-ordered-line-means-at-the-packet-level)
- [QUIC's fix, at the same packet level](#quics-fix-at-the-same-packet-level)
- [Retransmission: stream offset vs. packet number](#retransmission-stream-offset-vs-packet-number)
- [QUIC's three handshake key levels](#quics-three-handshake-key-levels)
- [HTTP/2 vs HTTP/3](#http2-vs-http3)
- [Where does DTLS fit in a client-server connection?](#where-does-dtls-fit-in-a-client-server-connection)
- [What this repository actually measures](#what-this-repository-actually-measures)
- [Testing scenarios](#testing-scenarios)
- [How the test scripts work](#how-the-test-scripts-work)
- [Results](#results)
- [Running it yourself](#running-it-yourself)

## Why a handshake exists at all

Suppose you and a stranger need to agree on a secret codeword, but the only
way to communicate is by shouting across a crowded room where anyone can
hear you. You can't just shout the codeword — everyone would know it. You
need some exchange of information, in public, that lets the two of you both
arrive at the same secret while anyone eavesdropping ends up with nothing
usable.

That's the situation a browser and a web server are in every time a new
HTTPS connection starts. They have never met. The network between them —
your ISP, transit providers, the server's ISP — can read every unencrypted
byte. Yet by the end of a short exchange, both sides hold an identical
symmetric encryption key that nobody who only watched the exchange can
derive. This is what a *handshake* is: a protocol for two parties to agree
on a shared secret over a channel a third party can observe.

TLS (the "S" in HTTPS) is the protocol that does this. It uses public-key
cryptography (Diffie-Hellman key exchange, specifically) so that even a
perfect eavesdropper who records the entire handshake cannot compute the
resulting shared secret. That part is not the problem this repository is
about — TLS's cryptography is sound and unchanged in spirit between the old
world and the new. What changed is *how many round trips it takes* and
*what happens to that connection afterwards*.

## Two separate problems with HTTPS over TCP

Classic HTTPS is TLS running on top of TCP. That pairing has two distinct
weaknesses. It's important to keep them separate, because QUIC fixes them
for two different reasons and the fixes are easy to conflate.

**Problem A — the handshake costs two sequential round trips, and that cost
is fixed.** TCP and TLS are layered, and layering means sequencing: TCP
doesn't know or care that TLS is about to run on top of it, so it completes
its own three-way handshake (SYN, SYN-ACK, ACK) *first*, establishing a
reliable ordered pipe, before TLS gets to send a single byte. Only then does
TLS run its own handshake on top. Two protocols, two round trips, paid in
full on every fresh connection — regardless of how fast, slow, lossy, or
clean the network is. This cost doesn't get worse under bad conditions; it's
just always there.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    Note over C,S: TCP handshake (round trip 1)
    C->>S: SYN
    S->>C: SYN-ACK
    C->>S: ACK
    Note over C,S: TLS 1.3 handshake (round trip 2)
    C->>S: ClientHello (key share)
    S->>C: ServerHello, cert, Finished
    C->>S: Finished
    Note over C,S: Application data can now flow
    C->>S: HTTP request
```

**Problem B — a single lost packet blocks every stream sharing the
connection, and that cost is conditional.** Modern HTTP/2 multiplexes many
logical requests (say, a dozen images and API calls) over one TCP
connection to avoid paying Problem A's cost repeatedly. But TCP guarantees
one thing and one thing only: bytes arrive at the application **in the
order they were sent, with nothing missing**. If packet 47 of 200 is lost,
TCP will not hand *any* of packets 48 through 200 to the application — not
even the ones for a completely unrelated HTTP/2 stream — until a
retransmission of packet 47 arrives and fills the gap. This is
**head-of-line (HOL) blocking**, and unlike Problem A, its cost is
*conditional*: it depends on the loss rate, the RTT (how long a
retransmission takes to arrive), and how many independent streams happen to
be sharing that one connection when the loss occurs.

```mermaid
flowchart TB
    subgraph before["Before: 3 streams multiplexed, packet 2 lost"]
        direction LR
        A1["Stream A<br/>pkt 1"] --> A2["Stream B<br/>pkt 2 LOST"] --> A3["Stream C<br/>pkt 3"] --> A4["Stream A<br/>pkt 4"]
    end
    subgraph after["What the application sees"]
        direction LR
        B1["Stream A<br/>delivered"] --> B2["Stream B: waiting...<br/>Stream C: waiting...<br/>Stream A pkt 4: waiting..."]
    end
    before --> after
```

## What "one ordered line" means, at the packet level

It's worth being precise about *why* TCP behaves this way, because the
mechanism is what QUIC actually redesigns.

TCP delivers a single ordered byte stream to the application. The kernel's
TCP receive buffer holds every segment that arrives, but it will not release
bytes to the reading application past the first gap. If segments carrying
bytes 1000–1999 and 3000–3999 have arrived but 2000–2999 has not, the
application can read bytes up to 999 and then blocks — the 3000–3999 bytes
are sitting in the kernel buffer, fully received, but withheld until 2000
arrives.

This interacts badly with TLS. TLS turns the byte stream into a sequence of
*records*, each individually encrypted. The AEAD (authenticated encryption)
scheme TLS uses needs a nonce for each record, and TLS derives that nonce
implicitly from a **record sequence counter** — record 0 uses nonce
`base XOR 0`, record 1 uses `base XOR 1`, and so on. Nothing in the record
itself states which number it is; both sides just count. That means to
decrypt record N+1 correctly, you must have already counted through record
N — which, combined with TCP's ordered-delivery guarantee, means the
byte-level blocking above becomes a decryption-level blocking too. There's
no way to skip ahead.

## QUIC's fix, at the same packet level

QUIC runs over UDP, and each UDP datagram carries exactly one QUIC packet
(with rare coalescing exceptions during the handshake). Critically, **every
QUIC packet header carries its own explicit packet number** — not an
implicit counter both sides track, but a number written directly into the
packet. QUIC's AEAD nonce is derived directly from that explicit number, not
from a count of how many packets arrived before it.

The consequence: a QUIC packet can be decrypted the instant it arrives,
independent of whatever order it showed up in. There is no "waiting to count
up to N" — the packet announces its own N. Loss of one packet no longer
creates a decryption dependency for any other packet, on any stream.

```mermaid
flowchart TB
    subgraph tcp["TCP+TLS: implicit, count-based nonce"]
        direction TB
        T0["Record 0<br/>nonce = base XOR 0"] --> T1["Record 1<br/>nonce = base XOR 1<br/>(needs record 0 counted first)"] --> T2["Record 2<br/>nonce = base XOR 2<br/>(needs 0 and 1 counted first)"]
    end
    subgraph quic["QUIC: explicit, self-describing nonce"]
        direction TB
        Q0["Packet #58<br/>nonce = f(58)<br/>decrypts alone"]
        Q1["Packet #59<br/>nonce = f(59)<br/>decrypts alone"]
        Q2["Packet #60<br/>nonce = f(60)<br/>decrypts alone"]
    end
```

QUIC then layers *streams* on top of these independently-decryptable
packets, each stream with its own flow control and its own delivery order
guarantee — but that guarantee applies **per stream**, not connection-wide.
Losing a packet belonging to stream B has no effect on the deliverability of
packets belonging to stream A or C, because nothing about decrypting or
processing A's packets ever depended on B's.

## Retransmission: stream offset vs. packet number

If packet numbers are never reused, how does QUIC retransmit lost data
without breaking the nonce scheme? By keeping two coordinates for data that
are deliberately independent:

- **Stream offset** — the logical byte position within a stream's byte
  sequence. "This chunk is bytes 4096–8191 of stream 4." This is what the
  receiving application ultimately cares about, and it never changes for a
  given piece of data no matter how many times it's retransmitted.
- **Packet number** — which physical transmission attempt carried that data
  over the wire. Strictly increasing, never reused, one per QUIC packet
  actually sent.

When a packet is lost, QUIC doesn't "resend packet #58." It takes the
stream data that was in #58, puts it in a brand new packet — say, #71 —
with its own new packet number and therefore its own freshly derived nonce,
and sends that instead. The receiver doesn't care that this data arrived in
packet #71 instead of #58; it reads the STREAM frame's offset field, sees
"this is bytes 4096–8191 of stream 4," and slots it into the correct
position in that stream's buffer regardless of which packet number
delivered it or what order it arrived in relative to other streams.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    C->>S: Packet #58 (stream 4, offset 4096-8191)
    Note over S: Packet #58 lost in transit
    C->>S: Packet #59 (stream 7, offset 0-2047)
    S-->>C: ACK #59 (stream 7 data delivered immediately)
    Note over C: Loss detected (no ACK for #58)
    C->>S: Packet #71 (stream 4, offset 4096-8191, NEW packet number)
    Note over S: Receiver uses offset, not packet number,<br/>to place this data correctly in stream 4
    S-->>C: ACK #71
```

## QUIC's three handshake key levels

QUIC's handshake derives three separate sets of encryption keys as it
progresses, each protecting a different part of the exchange:

| Level | Protects | Derived from |
|---|---|---|
| **Initial** | The very first packets (ClientHello, ServerHello) | A key derived from the QUIC connection ID — weak, but enough to deter casual on-path tampering before real crypto exists |
| **Handshake** | Certificate, certificate verify, handshake Finished messages | The (EC)DHE key exchange, once both sides have exchanged key shares |
| **1-RTT** (Application) | All actual application data | The final negotiated master secret, same cryptographic strength as any TLS 1.3 session |

This maps directly onto TLS 1.3's own key schedule — QUIC doesn't invent new
cryptography, it *carries* TLS 1.3 inside its packets, encrypted with keys
appropriate to how far the handshake has progressed.

## HTTP/2 vs HTTP/3

It's easy to assume HTTP/3 is "HTTP/2 but faster." The relationship is more
specific than that:

- **HTTP/2 runs on TCP.** Its major innovation over HTTP/1.1 was framing —
  multiplexing many requests over one connection instead of one request at
  a time (or many parallel TCP connections as a workaround). It's still
  exposed to both Problem A and Problem B above, because it's still TCP
  underneath.
- **HTTP/3 requires QUIC, and QUIC requires UDP.** There is no "HTTP/3 over
  TCP" — HTTP/3's entire benefit comes from QUIC's transport properties, so
  the two are inseparable. If UDP is unavailable, HTTP/3 is unavailable,
  full stop.

Because a client can't know in advance whether a server supports HTTP/3 (and
by extension, whether the network path even permits UDP to it), browsers use
an opportunistic upgrade mechanism called **Alt-Svc**. The first connection
to any given host is always plain HTTP/2 (or HTTP/1.1) over TCP, because
that's the only thing guaranteed to work. If the server's response includes
an `Alt-Svc` header advertising HTTP/3 support, the client remembers this
and, for **subsequent** connections to that host, races a QUIC attempt
against a TCP fallback — using whichever completes successfully. Clients on
networks that block or drop UDP (some corporate firewalls, some mobile
carriers) never see the QUIC attempt succeed, and simply stay on HTTP/2
indefinitely. This is a **permanent steady state** for that network, not a
temporary degraded mode waiting to recover.

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server (supports HTTP/3)
    Note over C,S: First-ever connection to this host
    C->>S: TCP handshake + TLS (HTTP/2)
    S-->>C: Response + Alt-Svc: h3=":443"
    Note over C: Client now knows this host offers HTTP/3
    Note over C,S: Next connection to the same host
    par Race
        C->>S: QUIC handshake attempt (UDP)
    and
        C->>S: TCP+TLS handshake attempt (fallback)
    end
    Note over C,S: Whichever completes first wins.<br/>If UDP is blocked on this network,<br/>TCP always wins, permanently.
```

## Where does DTLS fit in a client-server connection?

Everything above compares two ways of securing a **web** connection. There's
a third protocol worth placing on the map, because it's easy to mistake for
a QUIC alternative when it's actually solving a much narrower problem:
**DTLS** (Datagram Transport Layer Security).

DTLS is TLS adapted to run over an unreliable, unordered transport —
normally UDP — instead of TCP's ordered byte stream. It exists because
TLS's record layer assumes reliable in-order delivery: recall from earlier
that TLS derives each record's decryption nonce from an **implicit,
counted** sequence number, which only works if you've received every prior
record in order. DTLS fixes exactly that, and only that, by putting an
**explicit sequence number directly in each record's header** — the same
architectural idea QUIC uses for its packet numbers, except DTLS did it
first (DTLS 1.0 dates to 2006; QUIC to 2016).

That similarity raises a fair question: if DTLS already solved the
implicit-nonce problem over UDP, why did QUIC need to exist at all? Because
DTLS only ever solved the **crypto layer**. It hands you one encrypted,
unordered, unreliable datagram pipe and stops there. It does not provide:

- **Multiple multiplexed streams** — DTLS has no concept of "stream" at
  all. If an application needs several independent logical channels, it has
  to build that itself on top (WebRTC does exactly this: SCTP-over-DTLS,
  where SCTP supplies multi-streaming and DTLS supplies only the
  encryption).
- **Reliability or retransmission for application data** — DTLS's own
  retransmission logic covers just the *handshake* (which must complete
  reliably); once the connection is up, application data is send-and-forget
  unless the application adds its own retry logic.
- **Congestion control or connection migration** — both absent, both left
  to whatever the application layers on top.

So the useful mental model isn't "DTLS is a QUIC alternative" — it's
**DTLS occupies the same slot as TLS, not the same slot as QUIC.** QUIC
bundles security *and* reliable multiplexed transport into one integrated
protocol; DTLS only ever replaces the "TLS" box, leaving every transport
concern to whatever protocol is chosen to sit alongside it:

```mermaid
flowchart LR
    subgraph http2["Web browsing: HTTP/2"]
        direction TB
        A1["HTTP/2<br/>(framing, multiplexing)"] --> A2["TLS<br/>(security)"] --> A3["TCP<br/>(reliable, ordered transport)"] --> A4["IP"]
    end
    subgraph http3["Web browsing: HTTP/3"]
        direction TB
        B1["HTTP/3<br/>(framing)"] --> B2["QUIC<br/>(security + reliable,<br/>multiplexed transport -- combined)"] --> B3["UDP"] --> B4["IP"]
    end
    subgraph coap["IoT request/response: CoAP"]
        direction TB
        C1["CoAP<br/>(request/response)"] --> C2["DTLS<br/>(security only)"] --> C3["UDP"] --> C4["IP"]
    end
```

Notice DTLS sits in exactly the box TLS occupies in the HTTP/2 stack — not
in the box QUIC occupies in the HTTP/3 stack. **CoAP** (Constrained
Application Protocol) is a good concrete example to anchor this: it's a
lightweight HTTP-like request/response protocol built for IoT devices and
constrained networks, and "CoAP over DTLS" (sometimes written "CoAPs") is
directly analogous to "HTTP over TLS," just for a client-server exchange
that doesn't need HTTP/2-style multiplexing in the first place — one
request, one response, over UDP, secured by DTLS. Here's what that
connection actually looks like, including the round trip that's specific to
DTLS 1.2 — a stateless cookie exchange, required before the server commits
any real resources to the handshake, specifically to prevent DTLS being
abused for UDP source-address-spoofing amplification attacks:

```mermaid
sequenceDiagram
    participant C as IoT Client
    participant S as Cloud Server
    Note over C,S: DTLS 1.2 round trip 1: stateless cookie exchange
    C->>S: ClientHello (no cookie)
    S->>C: HelloVerifyRequest (cookie)
    Note over C,S: DTLS 1.2 round trip 2: real handshake
    C->>S: ClientHello (with cookie)
    S->>C: ServerHello, cert, ServerHelloDone
    C->>S: ClientKeyExchange, Finished
    S->>C: Finished
    Note over C,S: Secure channel established
    C->>S: CoAP GET /sensors/temperature
    S->>C: CoAP 2.05 Content: 21.4
```

That's already two round trips before any application data moves — on top
of a transport (UDP) that's supposed to be the "fast" one. This is the same
lesson as Problem A, told a different way: switching to UDP alone buys
nothing; QUIC's actual innovation is folding transport and crypto
negotiation into one integrated exchange, which DTLS-as-a-bolt-on-layer
doesn't do.

DTLS shows up in a few other real deployments worth knowing, beyond CoAP:
**WebRTC** uses DTLS-SRTP to secure audio/video and SCTP-over-DTLS for its
data channels (after peers find each other via a separate signaling step,
typically plain HTTPS), and some **VPN** products (OpenConnect/AnyConnect
being the best-known) run their bulk data tunnel over DTLS after an initial
TLS-over-TCP control connection, specifically to avoid the performance
problems of tunneling TCP traffic inside another TCP connection.

## What this repository actually measures

Given all of the above, this repository's live test demonstrates one
specific, narrow claim directly, with real packets on a real internet path:
**when packet loss delays a stream, TCP+TLS tends to drag down every other
stream sharing that connection along with it, while QUIC tends to delay
only the stream that actually lost a packet.** It measures this by opening
several concurrent streams over one connection (for both protocols),
sending data on all of them simultaneously, and recording the wall-clock
time each individual stream took to finish — first with a clean path, then
with real loss induced on the server's network interface. The number that
matters isn't just "how much slower did things get" — it's *how many
streams got pulled into that slowdown together* each time loss actually hit.
See [Results](#results) for why that distinction matters and how it's
measured.

DTLS is also measured, but only at the connection-establishment level
alongside TCP+TLS and QUIC — both a clean-path baseline and completion time
under the same induced loss. It's deliberately **not** included in the
per-stream HOL-blocking comparison above, for the reason covered in
[Where does DTLS fit in a client-server connection?](#where-does-dtls-fit-in-a-client-server-connection):
DTLS has no stream concept, so there's nothing for a lost packet to isolate
damage *away from* — the comparison wouldn't measure DTLS, it would measure
whatever ad hoc multiplexing scheme got bolted on top of it.

## Testing scenarios

A single place listing every scenario this repo measures (or intends to),
separate from the narrative sections above.

**Implemented:**

- **Handshake cost** — TCP+TLS vs QUIC vs DTLS, 15 (TCP/QUIC) or 5
  (DTLS, per condition) fresh-connection trials, clean path and under 5%
  induced loss. See [Handshake cost (Problem A)](#handshake-cost-problem-a).
- **Head-of-line blocking** — TCP+TLS vs QUIC, 4 concurrent streams per
  connection, single small payload per stream, 40 trials per protocol
  clean and 40 more under 5% induced loss. See
  [Head-of-line blocking (Problem B)](#head-of-line-blocking-problem-b).

**TODO:**

- **Naked UDP burst vs. QUIC multishot send** — as suggested by Pavel: add
  a raw/unencrypted UDP baseline (no TLS, no QUIC framing — the
  theoretical floor) and drive it with a real *series* of packets sent in
  a burst, not just 2-3. Compare that against QUIC pushed through the same
  kind of burst (parallel streams, sent via io_uring multishot/batched
  submission rather than one send syscall per packet). The hypothesis
  being tested: QUIC should pull ahead of naive raw UDP specifically
  under parallel/bulk dispatch — its stream multiplexing and packet
  pipelining are built for many packets in flight at once — whereas a
  naive one-packet-at-a-time raw UDP loop gets no benefit from that
  design. Not yet implemented; tracked here for a separate pass.

## How the test scripts work

```mermaid
flowchart TD
    subgraph london["London (this laptop, macOS)"]
        client["src/client.py<br/>hol subcommand"]
    end
    subgraph finland["cherry — Hetzner VPS, Helsinki (Linux)"]
        tcpsrv["src/tcp_server.py<br/>TCP+TLS echo, :5001"]
        quicsrv["src/quic_server.py<br/>QUIC echo, :4433"]
        netem["tc netem on eth0<br/>(loss injection)"]
    end
    client -->|"N concurrent streams,<br/>same connection"| tcpsrv
    client -->|"N concurrent streams,<br/>same connection"| quicsrv
    client -.->|"SSH: toggle loss<br/>before each condition"| netem
    netem -.->|"drops packets to/from<br/>both servers"| tcpsrv
    netem -.->|"drops packets to/from<br/>both servers"| quicsrv
```

`src/measure.py` orchestrates the whole run:

1. For each **condition** (`baseline`, then `lossy`): SSH into cherry and
   either clear or install a `tc qdisc ... netem loss <pct>%` rule on
   `eth0` — the interface facing the real internet, so loss applies
   symmetrically to both the TCP+TLS and QUIC test traffic.
2. For each **protocol** (`tcp`, `quic`) under that condition, it runs
   `client.py`'s `hol` subcommand *N* times: open one connection, fire off
   several concurrent streams of equal size, and record each stream's
   completion timestamp relative to the start.
3. Every stream's completion time from every trial is written to
   `results/hol_results.csv` (long format: one row per stream per trial).
4. Three charts are rendered to `docs/images/`: a bar chart classifying,
   for each lossy trial, how many of the 4 streams were delayed together
   (`hol_delay_distribution.png` — the primary evidence, see
   [Results](#results)); a box plot of the naive fastest-vs-slowest spread
   per trial (`hol_spread.png`); and a comparison of unaffected streams'
   completion times against baseline, restricted to cleanly-isolated
   single-loss trials (`hol_collateral.png`).

The echo servers (`tcp_server.py`, `quic_server.py`) are intentionally
trivial: each just echoes back whatever stream data it receives, so the
client can measure pure round-trip completion time without any server-side
processing delay confounding the numbers. `client.py` also has `handshake`
and `throughput` subcommands used to produce the connection-setup and
single-stream throughput numbers referenced above.

## Results

All numbers below are from real runs of this repository's own tests, London
laptop to the Helsinki VPS, captured on 2026-07-19. Raw data is in
[`results/hol_results.csv`](results/hol_results.csv) (regenerate with
`python src/measure.py`) and
[`results/handshake_results.csv`](results/handshake_results.csv)
(regenerate with `python src/measure_handshake.py`).

### Handshake cost (Problem A)

15 fresh-connection trials per protocol, no loss:

| | min | median | max |
|---|---|---|---|
| TCP+TLS | 97.6ms | **100.7ms** | 113.4ms |
| QUIC | 62.6ms | **71.6ms** | 165.5ms |

The path's baseline round trip time (measured separately via `ping`) is
~45–55ms — so TCP+TLS's ~101ms median lines up almost exactly with "two
sequential round trips" (TCP's own handshake, then TLS's, back to back),
while QUIC's ~72ms median reflects folding transport and crypto negotiation
into essentially one round trip's worth of wall-clock time. QUIC's max
(165.5ms) shows real-world jitter exists on both sides of this comparison —
this is a live internet path, not a lab bench.

#### Adding DTLS: a three-way comparison, and a real debugging trail

Extending this to DTLS (`src/measure_handshake.py`) took several honest
detours worth recording, because each one is itself informative about
testing real protocols on a real network rather than a lab bench:

1. **Real ambient network noise.** An early run produced numbers 2–5x worse
   across *all three* protocols, including baseline. `ping` explained why:
   the London–Helsinki path was independently experiencing 10–33% ambient
   loss and multi-second RTT spikes at that moment, unrelated to anything
   this repo was doing. It recovered on its own within minutes.
2. **A real bug in this test's own harness.** `dtls_handshake_once`
   originally `SIGKILL`ed the `openssl s_client` subprocess right after
   capturing the handshake-complete timestamp, to avoid measuring DTLS's
   own (slow, irrelevant) shutdown teardown. But killing it mid-shutdown
   left `openssl s_server` on the other end holding half-torn-down
   connection state — confirmed by a clear failure signature (trials
   succeed for a while, then fail in complete blocks) that had nothing to
   do with network conditions. Fixed by letting the client shut down
   gracefully in the background instead of killing it.
3. **A self-inflicted SSH rate limit.** Restarting the DTLS server between
   every attempt (to rule out server-side state as a variable) meant enough
   rapid SSH connections that `ufw`'s built-in brute-force limiter
   (`22/tcp LIMIT`, visible in `SETUP.md`) started refusing SSH outright —
   `Connection refused`, not a timeout. Fixed by pacing attempts further
   apart and not restarting the server on every single one.
4. **A genuine, only-partially-explained protocol-implementation finding.**
   Two different `openssl s_server -dtls1_2` instances, run with the
   identical command, behaved differently: one consistently performed the
   full cookie exchange (`openssl s_client -dtls1_2 -state` showed a
   duplicated `ClientHello`, `HelloVerifyRequest`, then the real handshake —
   matching the [DTLS 1.2 sequence diagram](#where-does-dtls-fit-in-a-client-server-connection)
   earlier, at a cost of ~150ms), while a second instance — after a
   restart — consistently skipped it entirely (`-state` showed a single
   `ClientHello` straight through to a valid session, at ~55ms). This is
   confirmed directly via two separate `-state` traces, not inferred from
   noisy timing data. DTLS 1.2's cookie exchange is *optional* by spec (RFC
   6347) — a server may skip it — so this is plausibly OpenSSL choosing
   differently between the two instances rather than a bug, but this
   repo's data can't say definitively *why* one instance made a different
   choice than the other. That's an honest limit of testing against one
   reference implementation from the outside, not a gap worth papering
   over.

The final dataset below is aggregated from 10 independent attempts (5
trials/protocol/condition each, 300 rows total, zero failures) against the
server instance that skips the cookie exchange — the one currently
running:

![Handshake cost: TCP+TLS vs QUIC vs DTLS](docs/images/handshake_comparison.png)

| | baseline min | baseline median | baseline mean | baseline max | lossy min | lossy median | lossy mean | lossy max |
|---|---|---|---|---|---|---|---|---|
| TCP+TLS | 92.1ms | 95.6ms | 116.4ms | 618.0ms | 91.1ms | 95.8ms | 130.0ms | 1100.1ms |
| QUIC | 54.5ms | 64.1ms | 66.6ms | 112.8ms | 56.2ms | 63.6ms | 77.2ms | 204.0ms |
| DTLS | 52.2ms | 56.9ms | 58.2ms | 73.8ms | 51.4ms | 57.5ms | 177.9ms | 3068.1ms |

Two things stand out across all three protocols, and neither is what a
first guess would predict:

- **On a clean path, the ordering isn't what the sequence diagrams alone
  would suggest.** QUIC (median 64.1ms) beating TCP+TLS (95.6ms) matches
  the round-trip-count story from earlier — one folded exchange against two
  sequential ones. But DTLS's median (56.9ms) comes in **lower than QUIC's
  too**, not in between QUIC and TCP+TLS as the DTLS 1.2 sequence diagram
  earlier would imply. That's because, for this server instance, there's no
  cookie round trip actually happening. The "DTLS costs an extra round
  trip" story is real and reproducible (see finding #4 above) — but only
  when the server chooses to enforce it, and here it doesn't.
- **Under loss, all three protocols get worse, but by very different
  amounts, and the size of the effect doesn't track the clean-path
  ordering at all.** TCP+TLS's mean rose the least in relative terms
  (116.4→130.0ms, +12%), with its worst case roughly doubling
  (618→1100ms). QUIC moved a bit more (66.6→77.2ms, +16% mean;
  112.8→204ms max, also roughly doubling). DTLS — the protocol with the
  *best* clean-path median of the three — reacted to loss far worse than
  either: its mean nearly tripled (58.2→177.9ms, +206%) and its worst case
  blew out 42x (73.8→3068.1ms), driven by rare but severe outliers rather
  than a uniform shift. Reporting only the median would hide the most
  operationally relevant fact here: whatever server-side machinery makes
  DTLS's cookie-free path fast on a clean connection recovers far more
  slowly than either TCP+TLS or QUIC when a handshake packet is actually
  lost.

All of these are genuine measurements, not artifacts of the debugging
trail above — each was independently reproduced across 10 separate
attempts run at different times, well after every known harness bug was
fixed.

### Head-of-line blocking (Problem B)

Methodology: open one connection, fire 4 concurrent streams of a single
small payload each (~1 packet up, 1 packet back per stream) simultaneously,
record each stream's own completion time. 40 trials per protocol with no
loss (baseline), 40 more with 5% packet loss induced on the server's real
network interface (lossy). A trial "shows delay" when at least one stream
finishes more than 15ms after that trial's fastest stream — enough margin
above baseline jitter (typically 0-10ms) to indicate an actual
retransmission occurred, not just normal variance.

The question that isolates head-of-line blocking specifically is: **when a
loss event does cause a delay, how many of the 4 streams does it drag down
with it?** If only the stream that actually lost a packet is slow, the
other streams are unaffected — no HOL blocking. If most or all of the
streams are slow together, the connection is serializing everyone behind
one loss — that's HOL blocking.

![How many streams does a loss event delay](docs/images/hol_delay_distribution.png)

| Streams delayed together | TCP+TLS trials | QUIC trials |
|---|---|---|
| 0 of 4 (no delay) | 31 | 23 |
| 1 of 4 (isolated) | 3 | **11** |
| 2 of 4 | 0 | 5 |
| 3 of 4 | **6** | 1 |
| 4 of 4 | 0 | 0 |

Of the trials that showed any delay at all (TCP: 9/40, QUIC: 17/40), TCP+TLS
dragged down **3 of the 4 streams together in two-thirds of those cases**
(6 of 9) and isolated the damage to just one stream in only 3. QUIC shows
almost the exact inverse: **exactly one stream was affected in nearly
two-thirds of its delayed trials** (11 of 17), with multi-stream delays
being comparatively rare. This is the real, measured signature of
connection-wide head-of-line blocking on TCP+TLS versus per-stream isolation
on QUIC, on a live internet path with real, uncontrolled packet loss — not
a simulation.

For context, here's the same data as the naive "spread" metric (fastest
vs. slowest stream in each trial). It's included because it's the first
thing you'd think to plot, and it's worth seeing *why it's not sufficient
on its own*: a stream that loses its own packet gets slow on both
protocols (that's just normal loss recovery, not HOL blocking), so raw
spread mixes "this stream directly lost a packet" together with "this
stream was collateral damage from another stream's loss" — exactly the two
things the chart above was built to tell apart.

![Per-trial spread](docs/images/hol_spread.png)

A secondary view: restricting to trials cleanly classified as "exactly one
stream affected, the other three tightly clustered," and comparing those
three unaffected streams' completion times against each protocol's own
loss-free baseline:

![Collateral damage on unaffected streams](docs/images/hol_collateral.png)

QUIC had 11 such cleanly-isolated trials (33 unaffected-stream samples);
their median completion time (64.2ms) sits close to QUIC's own baseline
median (56.6ms). TCP+TLS had only 1 such trial (3 samples) — not because
TCP is somehow better, but because the "exactly one stream affected"
pattern is rare on TCP in the first place, per the distribution chart above:
loss on TCP+TLS usually doesn't stay isolated long enough to produce a clean
one-straggler case to measure.

### Conclusions

**What this test proved, with real measurements on a real internet path:**

- **QUIC establishes a usable connection faster than TCP+TLS.** ~72ms
  median vs. ~101ms median on a path with ~45–55ms RTT — consistent with
  QUIC folding transport and crypto negotiation into roughly one round trip
  instead of two sequential ones. This is a direct, repeatable measurement,
  not an inference.
- **Packet loss on TCP+TLS tends to delay multiple streams together; packet
  loss on QUIC tends to stay confined to the one stream that lost a
  packet.** When a loss event caused any visible delay, TCP+TLS pulled 3 of
  4 streams down together in two-thirds of those cases; QUIC isolated the
  damage to exactly 1 of 4 streams in nearly two-thirds of its own cases.
  That's the inverse pattern you'd expect from "one ordered byte pipe" vs.
  "independently-decryptable packets across independent streams," and it
  showed up in real, uncontrolled loss on a real path — not a simulation
  built to make the point look clean.
- **DTLS's per-handshake cost depends on a server-side choice that this
  repo's data can observe but not fully explain.** Two `openssl s_server`
  instances, launched with the identical command, differed reproducibly:
  one always ran the DTLS 1.2 cookie exchange (~150ms), the other never did
  (~55ms) — confirmed directly via `-state` traces, not guessed from noisy
  timing alone. Whichever mode is active, DTLS's *tail* latency under real
  loss was consistently the worst of the three protocols (mean handshake
  time roughly tripled under 5% loss, versus a much smaller increase for
  TCP+TLS and QUIC), even in the fast/no-cookie mode. That asymmetry between
  "typical cost can look great" and "worst case is meaningfully worse" is a
  real, repeatable finding, not a byproduct of the debugging process that
  produced it.

**What this test did *not* prove, and shouldn't be read as showing:**

- **Statistical rigor beyond "directionally clear."** 15 handshake trials
  and 40 HOL trials per condition, on one real (and therefore noisy) path,
  is enough to see a strong, consistent pattern — it is not a
  publication-grade sample. The TCP "collateral damage" comparison in
  particular rests on just 1 cleanly-isolated trial (3 data points) and
  shouldn't be trusted as a precise number, only as consistent with the
  broader distribution chart.
- **Behavior across a matrix of conditions.** This ran at one loss rate
  (5%), one RTT (~50ms), one stream count (4), and one payload size
  (deliberately tiny, to isolate the HOL-blocking signal cleanly from
  direct per-stream loss recovery). It says nothing about how the gap
  changes at higher loss, longer RTT, more streams, or larger payloads —
  each would need its own run.
- **Real browser or CDN behavior.** No 0-RTT session resumption, no
  Alt-Svc racing, no real congestion-control tuning beyond each library's
  defaults, no concurrent unrelated traffic. This measures the two
  transport-layer mechanisms directly, with everything else stripped away
  — which is what makes the comparison clean, but also what makes it
  narrower than "how much faster will my website be on HTTP/3."
- **That QUIC is strictly better in every dimension.** QUIC's handshake max
  (165.5ms) exceeded TCP+TLS's max (113.4ms) in the 15-trial sample — real
  paths have jitter, and QUIC isn't immune to it. The claim this test
  supports is specifically about round-trip *count* and per-stream
  *isolation*, not "QUIC wins every single trial."
- **Why DTLS's two server instances chose differently on the cookie
  exchange.** This is observed and reproduced, not explained. It would take
  reading OpenSSL's own cookie-generation logic, or testing a second,
  independent DTLS implementation, to know whether this is
  OpenSSL-specific, configuration-dependent, or something else — squarely
  out of scope for a network-behavior benchmark like this one. Treat the
  DTLS numbers as "what this one reference implementation did, twice,
  differently" rather than "what DTLS 1.2 costs" in general.

## Running it yourself

See [SETUP.md](SETUP.md) for firewall configuration (Hetzner Cloud firewall
+ `ufw`) and the exact `tc netem` commands used to induce loss. In short:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# on your server:
scp src/{tcp_server.py,quic_server.py,cert.pem,key.pem} you@your-server:~/quic-vs-tcp/
ssh you@your-server "cd quic-vs-tcp && python3 tcp_server.py & python3 quic_server.py &"

# from your client machine:
python src/client.py handshake tcp <server-ip> 5001 --trials 10
python src/client.py handshake quic <server-ip> 4433 --trials 10
python src/measure.py --host <server-ip>
```
