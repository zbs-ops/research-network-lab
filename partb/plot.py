"""Turn Part B's CSVs into a summary table and plots.

usage: python partb/plot.py ARTIFACT_DIR OUT_DIR
Reads every results_rtt*.csv under ARTIFACT_DIR. Writes summary.csv (median of the
repetitions per configuration, plus min and max) and one PNG per question.
"""

from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

KEY = ("rtt_ms", "loss_pct", "cc", "mtu", "streams", "buffers")


def load(src: Path) -> list[dict]:
    rows = []
    for f in sorted(src.rglob("results_rtt*.csv")):
        with open(f) as fh:
            rows += list(csv.DictReader(fh))
    return rows


def summarize(rows: list[dict]) -> dict[tuple, dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in KEY)].append(r)
    out = {}
    for key, rs in groups.items():
        t = [float(r["throughput_mbps"]) for r in rs]
        out[key] = {
            **dict(zip(KEY, key)),
            "reps": len(rs),
            "median_mbps": round(statistics.median(t), 1),
            "min_mbps": round(min(t), 1),
            "max_mbps": round(max(t), 1),
            "median_retransmits": int(statistics.median(int(r["retransmits"]) for r in rs)),
            "measured_rtt_ms": rs[0]["measured_rtt_ms"],
        }
    return out


def value(summary: dict, rtt, loss, cc, mtu, streams, buffers="tuned"):
    s = summary.get((str(rtt), str(loss), cc, str(mtu), str(streams), buffers))
    return s["median_mbps"] if s else float("nan")


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(load(src))
    if not summary:
        raise SystemExit(f"no results under {src}")
    with open(out / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(summary.values())).keys()))
        w.writeheader()
        w.writerows(sorted(summary.values(), key=lambda s: tuple(s[k] for k in KEY)))

    rtts = sorted({float(k[0]) for k in summary})
    losses = sorted({float(k[1]) for k in summary})
    fmt = lambda x: str(int(x)) if float(x).is_integer() else str(x)  # noqa: E731

    # 1. CUBIC vs BBR as loss grows, single stream, MTU 1500, one panel per RTT.
    fig, axes = plt.subplots(1, len(rtts), figsize=(4.2 * len(rtts), 3.6), sharey=True)
    for ax, rtt in zip(axes, rtts):
        for cc in ("cubic", "bbr"):
            ax.plot([fmt(l) for l in losses], [value(summary, fmt(rtt), fmt(l), cc, 1500, 1) for l in losses],
                    marker="o", label=cc.upper())  # fmt: skip
        ax.set_title(f"RTT {fmt(rtt)} ms")
        ax.set_xlabel("packet loss (%)")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("throughput (Mbit/s, median)")
    axes[0].legend()
    fig.suptitle("Single TCP stream over a 1 Gbit/s path: CUBIC vs BBR")
    fig.tight_layout()
    fig.savefig(out / "cubic_vs_bbr.png", dpi=150)

    # 2. Parallel streams and jumbo frames at the longest RTT with some loss.
    rtt = fmt(rtts[-1])
    loss = fmt(losses[1] if len(losses) > 1 else losses[0])
    labels, bars = [], []
    for cc in ("cubic", "bbr"):
        for mtu in (1500, 9000):
            for streams in (1, 4):
                labels.append(f"{cc.upper()}\nMTU {mtu}\n{streams} stream{'s' if streams > 1 else ''}")
                bars.append(value(summary, rtt, loss, cc, mtu, streams))
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(range(len(bars)), bars, color=["#4c72b0"] * 4 + ["#dd8452"] * 4)
    ax.set_xticks(range(len(bars)), labels, fontsize=8)
    ax.set_ylabel("throughput (Mbit/s, median)")
    ax.set_title(f"RTT {rtt} ms, loss {loss}%: what MTU and parallel streams buy")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "mtu_and_streams.png", dpi=150)

    # 3. Default vs tuned socket buffers, clean path, single stream, MTU 1500.
    fig, ax = plt.subplots(figsize=(6, 3.6))
    for cc, buf, style in (("cubic", "default", "o--"), ("cubic", "tuned", "o-"), ("bbr", "default", "s--"), ("bbr", "tuned", "s-")):
        ax.plot([fmt(r) for r in rtts], [value(summary, fmt(r), "0", cc, 1500, 1, buf) for r in rtts], style,
                label=f"{cc.upper()}, {buf} buffers")  # fmt: skip
    ax.set_xlabel("round-trip time (ms)")
    ax.set_ylabel("throughput (Mbit/s, median)")
    ax.set_title("Socket buffer ceilings vs distance (no loss)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "buffers.png", dpi=150)
    print(f"{len(summary)} configurations summarized")


if __name__ == "__main__":
    main()
