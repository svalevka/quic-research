"""
Measures per-stream completion time for TCP+TLS and QUIC under normal and
lossy network conditions, over the real client<->cherry path.

Loss is injected on cherry's egress interface (eth0) via tc netem, since the
client here is macOS and has no tc. For each (protocol, condition) pair this
runs N repeated `hol` trials and records every stream's completion time.

Usage:
  python src/measure.py --host 37.27.21.13 --tcp-port 5001 --quic-port 4433 \
      --repeats 8 --streams 4 --bytes 262144 --chunk 16384
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


async def run_trial(proto: str, host: str, port: int, cafile: str, streams: int, nbytes: int, chunk: int):
    ns = argparse.Namespace(
        proto=proto, host=host, port=port, cafile=cafile,
        streams=streams, bytes=nbytes, chunk=chunk,
    )
    if proto == "tcp":
        return await quicvstcp.hol_tcp(ns)
    return await quicvstcp.hol_quic(ns)


async def collect(args):
    cafile = os.path.join(os.path.dirname(__file__), "cert.pem")
    rows = []

    conditions = [("baseline", None), ("lossy", args.loss_pct)]

    for condition, loss_pct in conditions:
        if loss_pct is not None:
            print(f"[measure] inducing {loss_pct}% loss on cherry:eth0")
            induce_loss(loss_pct)
            time.sleep(1)
        else:
            clear_loss()
            time.sleep(1)

        for proto, port in [("tcp", args.tcp_port), ("quic", args.quic_port)]:
            for trial in range(args.repeats):
                try:
                    completions = await run_trial(
                        proto, args.host, port, cafile, args.streams, args.bytes, args.chunk
                    )
                except Exception as exc:
                    print(f"  [{condition}/{proto}] trial {trial+1} FAILED: {exc}")
                    continue
                vals = list(completions.values())
                spread = (max(vals) - min(vals)) * 1000
                print(f"  [{condition}/{proto}] trial {trial+1}: spread={spread:.2f}ms")
                for sid, dt in completions.items():
                    rows.append({
                        "condition": condition,
                        "protocol": proto,
                        "trial": trial,
                        "stream_id": sid,
                        "completion_ms": dt * 1000,
                        "spread_ms": spread,
                    })

    clear_loss()
    return rows


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["condition", "protocol", "trial", "stream_id", "completion_ms", "spread_ms"])
        writer.writeheader()
        writer.writerows(rows)


def plot_spread(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(os.path.dirname(path), exist_ok=True)

    per_trial_spread = {}
    seen = set()
    for r in rows:
        key = (r["condition"], r["protocol"], r["trial"])
        if key in seen:
            continue
        seen.add(key)
        per_trial_spread.setdefault((r["condition"], r["protocol"]), []).append(r["spread_ms"])

    conditions = ["baseline", "lossy"]
    protocols = ["tcp", "quic"]
    labels = [f"{c}\n{p}" for c in conditions for p in protocols]
    data = [per_trial_spread.get((c, p), []) for c in conditions for p in protocols]

    fig, ax = plt.subplots(figsize=(8, 5))
    colors = ["#4C72B0", "#DD8452", "#4C72B0", "#DD8452"]
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_ylabel("Inter-stream completion spread (ms)\nmax(stream completion) - min(stream completion)")
    ax.set_title("Per-trial spread: TCP+TLS vs QUIC\nreal London <-> Finland path")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"[measure] wrote chart to {path}")


def classify_delay_distribution(rows, delay_threshold_ms=15.0):
    """
    For each lossy trial, count how many of the streams were delayed more
    than delay_threshold_ms above that trial's own fastest stream. This is
    the direct measure of head-of-line blocking: on TCP+TLS, a loss on any
    one stream tends to delay every stream sharing the connection; on QUIC,
    it tends to delay only the one stream whose packet was actually lost.
    Returns {protocol: {n_delayed: trial_count}}.
    """
    trials = {}
    for r in rows:
        key = (r["condition"], r["protocol"], r["trial"])
        trials.setdefault(key, []).append(r["completion_ms"])

    dist = {}
    for (condition, protocol, trial), vals in trials.items():
        if condition != "lossy":
            continue
        fastest = min(vals)
        n_delayed = sum(1 for v in vals if v - fastest > delay_threshold_ms)
        dist.setdefault(protocol, {}).setdefault(n_delayed, 0)
        dist[protocol][n_delayed] += 1
    return dist


def plot_delay_distribution(dist, n_streams, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    os.makedirs(os.path.dirname(path), exist_ok=True)

    protocols = ["tcp", "quic"]
    bucket_labels = [f"{k} of {n_streams}\nstreams delayed" for k in range(n_streams + 1)]
    x = np.arange(len(bucket_labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(9, 5.5))
    for i, proto in enumerate(protocols):
        counts = [dist.get(proto, {}).get(k, 0) for k in range(n_streams + 1)]
        offset = (i - 0.5) * width
        bars = ax.bar(x + offset, counts, width, label=proto, alpha=0.75)
        ax.bar_label(bars, padding=2)

    ax.set_xticks(x)
    ax.set_xticklabels(bucket_labels)
    ax.set_ylabel("Number of lossy trials")
    ax.set_title(
        "When a loss event delays a stream, how many streams does it take with it?\n"
        "TCP+TLS vs QUIC, real London <-> Finland path, 5% induced loss"
    )
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"[measure] wrote chart to {path}")


def classify_collateral(rows, straggler_gap_ms=15.0, others_tight_ms=15.0):
    """
    For each lossy trial, sort the 4 stream completion times. If the slowest
    stream is a clear outlier (straggler_gap_ms above the next-slowest) and
    the remaining streams are tightly clustered together (others_tight_ms
    apart), treat this as a trial where exactly one stream was hit by loss.
    Returns the completion times of the *other, unaffected* streams in those
    trials -- this isolates collateral damage from direct damage.
    """
    trials = {}
    for r in rows:
        key = (r["condition"], r["protocol"], r["trial"])
        trials.setdefault(key, []).append(r["completion_ms"])

    collateral = {}
    isolated_counts = {}
    for (condition, protocol, trial), vals in trials.items():
        if condition != "lossy":
            continue
        vals = sorted(vals)
        straggler = vals[-1]
        others = vals[:-1]
        if (straggler - others[-1] > straggler_gap_ms) and (others[-1] - others[0] < others_tight_ms):
            collateral.setdefault(protocol, []).extend(others)
            isolated_counts[protocol] = isolated_counts.get(protocol, 0) + 1

    return collateral, isolated_counts


def plot_collateral(rows, collateral, isolated_counts, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(os.path.dirname(path), exist_ok=True)

    baseline = {}
    for r in rows:
        if r["condition"] == "baseline":
            baseline.setdefault(r["protocol"], []).append(r["completion_ms"])

    protocols = ["tcp", "quic"]
    labels = []
    data = []
    for proto in protocols:
        labels.append(f"{proto}\nbaseline\n(all streams)")
        data.append(baseline.get(proto, []))
        n = isolated_counts.get(proto, 0)
        labels.append(f"{proto}\nlossy\n(unaffected streams,\nn={n} isolated trials)")
        data.append(collateral.get(proto, []))

    fig, ax = plt.subplots(figsize=(9, 5.5))
    colors = ["#4C72B0", "#4C72B0", "#DD8452", "#DD8452"]
    bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.6)

    ax.set_ylabel("Stream completion time (ms)")
    ax.set_title(
        "Collateral damage: streams that did NOT lose a packet themselves,\n"
        "in trials where exactly one other stream did\n"
        "real London <-> Finland path, isolated single-packet-loss trials only"
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"[measure] wrote chart to {path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="37.27.21.13")
    p.add_argument("--tcp-port", type=int, default=5001)
    p.add_argument("--quic-port", type=int, default=4433)
    p.add_argument("--repeats", type=int, default=40)
    p.add_argument("--streams", type=int, default=4)
    p.add_argument("--bytes", type=int, default=1000)
    p.add_argument("--chunk", type=int, default=1000)
    p.add_argument("--loss-pct", type=float, default=5.0)
    args = p.parse_args()

    rows = asyncio.run(collect(args))

    csv_path = os.path.join(RESULTS_DIR, "hol_results.csv")
    write_csv(rows, csv_path)
    print(f"[measure] wrote {len(rows)} rows to {csv_path}")

    plot_spread(rows, os.path.join(IMAGES_DIR, "hol_spread.png"))

    collateral, isolated_counts = classify_collateral(rows)
    for proto in ["tcp", "quic"]:
        base = sorted(r["completion_ms"] for r in rows if r["condition"] == "baseline" and r["protocol"] == proto)
        coll = sorted(collateral.get(proto, []))
        n_base, n_coll = len(base), len(coll)
        base_med = base[n_base // 2] if n_base else float("nan")
        coll_med = coll[n_coll // 2] if n_coll else float("nan")
        print(f"[measure] {proto}: isolated single-loss trials={isolated_counts.get(proto, 0)}, "
              f"baseline median={base_med:.1f}ms, collateral (unaffected streams) median={coll_med:.1f}ms")

    plot_collateral(rows, collateral, isolated_counts, os.path.join(IMAGES_DIR, "hol_collateral.png"))

    dist = classify_delay_distribution(rows)
    plot_delay_distribution(dist, args.streams, os.path.join(IMAGES_DIR, "hol_delay_distribution.png"))
    for proto in ["tcp", "quic"]:
        total = sum(dist.get(proto, {}).values())
        any_delay = sum(v for k, v in dist.get(proto, {}).items() if k > 0)
        print(f"[measure] {proto}: {any_delay}/{total} lossy trials showed any delay; breakdown: {dict(sorted(dist.get(proto, {}).items()))}")


if __name__ == "__main__":
    main()
