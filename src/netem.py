"""
Shared helper for toggling tc netem packet loss on cherry's real
internet-facing interface (eth0), over SSH. Used by both measure.py (HOL
blocking) and measure_handshake.py (connection-establishment cost).

The client here is macOS and has no tc, so loss is always injected
server-side -- see SETUP.md for the equivalent client-side commands if
your own client happens to be Linux.
"""
import subprocess
import time


def ssh_cherry(cmd: str, retries: int = 3, delay: float = 2.0):
    # SSH to cherry occasionally fails transiently (observed: exit 255 with
    # no other symptom, unrelated to path packet loss -- ICMP stayed clean
    # through the same window). Retrying beats letting a single flaky SSH
    # call abort an entire multi-minute benchmark run, and beats silently
    # swallowing the failure, which could leave a previous loss-condition's
    # qdisc rule active for a later "clean" condition.
    last_exc = None
    for attempt in range(retries):
        try:
            subprocess.run(["ssh", "cherry", cmd], check=True, capture_output=True, text=True)
            return
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(delay)
    raise last_exc


def induce_loss(pct: float, iface: str = "eth0"):
    ssh_cherry(f"sudo tc qdisc replace dev {iface} root netem loss {pct}%")


def clear_loss(iface: str = "eth0"):
    ssh_cherry(f"sudo tc qdisc del dev {iface} root netem 2>/dev/null || true")
