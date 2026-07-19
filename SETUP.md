# Setup

This describes how the test environment is configured: server firewall
rules, certificate generation, and how packet loss is injected for the
lossy-condition trials.

## Topology

- **Client**: your local machine, wherever it actually is. This repo's own
  test run used a home network in London. Runs `src/client.py` and
  `src/measure.py`.
- **Server**: a Linux VPS. This repo's own test run used a Hetzner Cloud
  instance (`hel1`, Helsinki) referred to as `cherry` in scripts and
  commands below. Runs `src/tcp_server.py` and `src/quic_server.py`.

`tc netem` (Linux-only) is what injects loss, so it always runs **on the
server**, regardless of what OS the client is. If your client happens to be
Linux too, you could inject loss there instead — the demo doesn't care which
end drops packets, since it's testing behavior on a real, currently-lossy
path either way.

## 1. Certificates

The test uses a self-signed cert. Private keys are never committed to this
repo (see `.gitignore`); generate your own:

```bash
openssl req -x509 -newkey rsa:2048 -days 90 -nodes \
  -keyout src/key.pem -out src/cert.pem \
  -subj "/CN=your-server-name" \
  -addext "subjectAltName=IP:<your-server-ip>,DNS:your-server-name"
```

The `subjectAltName` **must** include the IP address you'll actually connect
to — `client.py` verifies the server's certificate properly (it does not
disable verification), and without a matching SAN, verification will fail
with a hostname mismatch rather than silently succeeding.

Copy `cert.pem` and `key.pem` to the server alongside the server scripts.
`client.py` only needs `cert.pem` (as its trusted CA) — never copy
`key.pem` off the server.

## 2. Server-side firewall

### Hetzner Cloud firewall

If you're on Hetzner Cloud, traffic has to clear the cloud-level firewall
before it ever reaches the VM's own `ufw`. Using the `hcloud` CLI:

```bash
hcloud firewall create --name quic-test
hcloud firewall add-rule quic-test --direction in --protocol tcp --port 22 \
  --source-ips <your-ip>/32
hcloud firewall add-rule quic-test --direction in --protocol tcp --port 5001 \
  --source-ips <your-ip>/32
hcloud firewall add-rule quic-test --direction in --protocol udp --port 4433 \
  --source-ips <your-ip>/32
hcloud firewall apply-to-resource quic-test --type server --server <your-server-name>
```

Scoping every rule to `--source-ips <your-ip>/32` rather than `0.0.0.0/0` is
deliberate: this opens exactly the two test ports, only to you, rather than
exposing an unauthenticated echo server to the entire internet. **This IP
is your home connection's public egress IP, which most home ISPs assign
dynamically** — if it changes between sessions, these rules stop matching
and you'll see connection timeouts that look like a server problem but
aren't. Check your current IP (`curl -4 ifconfig.me`) against the firewall
rule before debugging anything else, and update the rule if it's drifted.

### ufw, on the server itself

```bash
sudo ufw allow 5001/tcp
sudo ufw allow 4433/udp
sudo ufw status
```

Belt-and-suspenders: even with the Hetzner firewall already scoping access,
`ufw` should independently only allow what's needed, in case the instance
is ever moved off Hetzner or the cloud firewall is loosened later.

## 3. Running the servers

```bash
# on the server
python3 -m venv venv && source venv/bin/activate
pip install aioquic

python3 tcp_server.py 5001 cert.pem key.pem &
python3 quic_server.py 4433 cert.pem key.pem &
```

Both are trivial echo servers — no request handling logic, just "send back
whatever arrived on this stream" — so the client's timing measurements
reflect network and protocol behavior, not server processing time.

## 4. Injecting packet loss

`src/measure.py` does this automatically via SSH before its lossy-condition
trials, but the underlying commands (run **on the server**, not the client)
are:

```bash
# induce loss on the server's real internet-facing interface
sudo tc qdisc replace dev eth0 root netem loss 5%

# ...run trials while this is active...

# remove it afterward -- don't leave a lossy qdisc on a box you still use
sudo tc qdisc del dev eth0 root netem
```

Replace `eth0` with whatever `ip route show default` reports as your
server's outbound interface. This drops packets on the real link, in both
directions, for all traffic through that interface — not a simulation, and
not scoped to only the test ports, so avoid running other latency-sensitive
work against the same box while a loss trial is active.

If your client is Linux and you'd rather inject loss client-side instead
(closer to "real world packet loss near the user" than "packet loss near
the server"), the equivalent is:

```bash
sudo tc qdisc replace dev <your-outbound-iface> root netem loss 5%
sudo tc qdisc del dev <your-outbound-iface> root netem
```

macOS has no `tc`; the closest equivalent is `pfctl` + `dnctl` (dummynet),
which uses an entirely different rule syntax and is not covered here, since
this repo's own test run injects loss server-side instead.

## 5. Keeping the firewall allowlist current

If your client's public IP changes (common on home broadband), re-run the
`hcloud firewall add-rule` command above with the new IP, or delete and
recreate the rule. Track your current egress IP with:

```bash
curl -4 ifconfig.me
```
