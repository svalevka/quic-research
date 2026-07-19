"""
Measures connection-establishment (handshake) cost for TCP+TLS, QUIC, and
DTLS, under normal and lossy network conditions, over the real
client<->cherry path.

This is the three-way extension of Problem A. It does NOT attempt to
compare per-stream head-of-line blocking for DTLS -- see the README section
"Where does DTLS fit in a client-server connection?" for why that
comparison wouldn't be meaningful (DTLS has no stream concept).

Usage:
  python src/measure_handshake.py --host 37.27.21.13 \
      --tcp-port 5001 --quic-port 4433 --dtls-port 4434 --trials 15
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import client as quicvstcp  # noqa: E402
from netem import induce_loss, clear_loss  # noqa: E402

import asyncio

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")
IMAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "images")

PROTOCOLS = ["tcp", "quic", "dtls"]


async def run_trial(proto: str, host: str, port: int, cafile: str):
    if proto == "tcp":
        return await quicvstcp.tcp_handshake_once(host, port, cafile)
    if proto == "quic":
        return await quicvstcp.quic_handshake_once(host, port, cafile)
    return await quicvstcp.dtls_handshake_once(host, port, cafile)


async def collect(args):
    cafile = os.path.join(os.path.dirname(__file__), "cert.pem")
    ports = {"tcp": args.tcp_port, "quic": args.quic_port, "dtls": args.dtls_port}
    rows = []

    conditions = [("baseline", None), ("lossy", args.loss_pct)]

    for condition, loss_pct in conditions:
        if loss_pct is not None:
            print(f"[measure_handshake] inducing {loss_pct}% loss on cherry:eth0")
            induce_loss(loss_pct)
            time.sleep(1)
        else:
            clear_loss()
            time.sleep(1)

        for proto in PROTOCOLS:
            for trial in range(args.trials):
                try:
                    dt = await run_trial(proto, args.host, ports[proto], cafile)
                except Exception as exc:
                    print(f"  [{condition}/{proto}] trial {trial+1} FAILED: {exc}")
                    continue
                duration_ms = dt * 1000
                print(f"  [{condition}/{proto}] trial {trial+1}: {duration_ms:.2f}ms")
                rows.append({
                    "condition": condition,
                    "protocol": proto,
                    "trial": trial,
                    "duration_ms": duration_ms,
                })

    clear_loss()
    return rows


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["condition", "protocol", "trial", "duration_ms"])
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows):
    for condition in ["baseline", "lossy"]:
        for proto in PROTOCOLS:
            vals = sorted(r["duration_ms"] for r in rows if r["condition"] == condition and r["protocol"] == proto)
            if not vals:
                print(f"[measure_handshake] {condition}/{proto}: no successful trials")
                continue
            n = len(vals)
            print(f"[measure_handshake] {condition}/{proto}: n={n} "
                  f"min={vals[0]:.1f}ms median={vals[n//2]:.1f}ms max={vals[-1]:.1f}ms")


def plot(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(os.path.dirname(path), exist_ok=True)

    conditions = ["baseline", "lossy"]
    labels = [f"{c}\n{p}" for c in conditions for p in PROTOCOLS]
    data = [
        [r["duration_ms"] for r in rows if r["condition"] == c and r["protocol"] == p]
        for c in conditions for p in PROTOCOLS
    ]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    colors = ["#4C72B0", "#DD8452", "#55A868"] * len(conditions)
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_ylabel("Handshake completion time (ms)")
    ax.set_title(
        "Connection-establishment cost: TCP+TLS vs QUIC vs DTLS\n"
        "real London <-> Finland path, baseline vs 5% induced loss"
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"[measure_handshake] wrote chart to {path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="37.27.21.13")
    p.add_argument("--tcp-port", type=int, default=5001)
    p.add_argument("--quic-port", type=int, default=4433)
    p.add_argument("--dtls-port", type=int, default=4434)
    p.add_argument("--trials", type=int, default=15)
    p.add_argument("--loss-pct", type=float, default=5.0)
    args = p.parse_args()

    rows = asyncio.run(collect(args))

    csv_path = os.path.join(RESULTS_DIR, "handshake_results.csv")
    write_csv(rows, csv_path)
    print(f"[measure_handshake] wrote {len(rows)} rows to {csv_path}")

    print_summary(rows)

    plot(rows, os.path.join(IMAGES_DIR, "handshake_comparison.png"))


if __name__ == "__main__":
    main()
