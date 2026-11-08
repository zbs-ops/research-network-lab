"""Part D figures, built only from the committed trial files (via analyze.py's recompute).

  python3 partd/plot.py results/partd --out results/partd/plots     # needs matplotlib

election_vs_delay.png   time to a new leader vs one-way delay (no loss), median and min-max
election_vs_loss.png    the same vs loss (no delay)
election_heatmap_<fault>.png   median time to a new leader over the delay x loss grid
partition_timeline.png  one trial per partition variant: acks (at arrival) and failures, by side
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from analyze import USABLE, load, recompute  # noqa: E402

# Categorical slots 1-2 of the reference palette (blue, orange); text and grid stay neutral.
TMO_COLOR = {"default": "#2a78d6", "tuned": "#eb6834"}
FAULT_MARK = {"kill": "o", "isolate": "s"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
OK_C, FAIL_C = "#2a78d6", "#e34948"

plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
                     "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "lines.linewidth": 2})


def series(rows, x_field, fixed_field, fixed_val):
    """{(members, timeouts, fault): {x: [election_s...]}} for ok trials with fixed_field == fixed_val."""
    out = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["exp"] != "election" or r.get("status") not in USABLE or r[fixed_field] != fixed_val:
            continue
        if r.get("election_s") is not None:
            out[(r["members"], r["timeouts"], r["fault"])][r[x_field]].append(r["election_s"])
    return out


def sweep(rows, x_field, fixed_field, xlabel, fname, out: Path):
    data = series(rows, x_field, fixed_field, 0)
    sizes = sorted({k[0] for k in data})
    if not sizes:
        return
    fig, axes = plt.subplots(1, len(sizes), figsize=(4.2 * len(sizes), 3.4), sharey=True, squeeze=False)
    for ax, n in zip(axes[0], sizes):
        for (m, tmo, fault), pts in sorted(data.items()):
            if m != n:
                continue
            xs = sorted(pts)
            med = [statistics.median(pts[x]) for x in xs]
            lo = [med[i] - min(pts[x]) for i, x in enumerate(xs)]
            hi = [max(pts[x]) - med[i] for i, x in enumerate(xs)]
            ax.errorbar(xs, med, yerr=[lo, hi], color=TMO_COLOR[tmo], marker=FAULT_MARK[fault], markersize=6,
                        linestyle="-" if fault == "kill" else "--", capsize=3, elinewidth=1,
                        label=f"{fault}, {tmo} timeouts")
        ax.set_title(f"{n} members", color=INK)
        ax.set_xlabel(xlabel)
        ax.set_ylim(bottom=0)
    axes[0][0].set_ylabel("time to a new leader (s)")
    axes[0][-1].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(out / fname, dpi=150)
    plt.close(fig)


def heatmaps(rows, out: Path):
    ok = [r for r in rows if r["exp"] == "election" and r.get("status") in USABLE]
    delays = sorted({r["delay_ms"] for r in ok})
    losses = sorted({r["loss_pct"] for r in ok})
    if len(delays) < 2 or len(losses) < 2:
        return
    for fault in sorted({r["fault"] for r in ok}):
        panels = sorted({(r["members"], r["timeouts"]) for r in ok if r["fault"] == fault})
        fig, axes = plt.subplots(1, len(panels), figsize=(3.3 * len(panels), 3.2), squeeze=False)
        for ax, (n, tmo) in zip(axes[0], panels):
            grid = []
            for lo in losses:
                row = []
                for d in delays:
                    v = [r["election_s"] for r in ok if (r["members"], r["timeouts"], r["fault"], r["delay_ms"],
                                                         r["loss_pct"]) == (n, tmo, fault, d, lo)
                         and r["election_s"] is not None]
                    row.append(statistics.median(v) if v else float("nan"))
                grid.append(row)
            im = ax.imshow(grid, cmap="Blues", origin="lower", aspect="auto", vmin=0)
            vmax = max((x for r in grid for x in r if x == x), default=1)
            for i, row in enumerate(grid):
                for j, v in enumerate(row):
                    if v == v:  # NaN (no trials in that cell) stays blank
                        ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                                color="white" if v > 0.6 * vmax else INK)
            ax.set_xticks(range(len(delays)), [str(d) for d in delays])
            ax.set_yticks(range(len(losses)), [str(x) for x in losses])
            ax.set_xlabel("one-way delay (ms)")
            ax.set_title(f"{n} members, {tmo}", color=INK)
            ax.grid(False)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        axes[0][0].set_ylabel("loss per direction (%)")
        fig.suptitle(f"median time to a new leader (s), leader {'killed' if fault == 'kill' else 'isolated'}",
                     color=INK)
        fig.tight_layout()
        fig.savefig(out / f"election_heatmap_{fault}.png", dpi=150)
        plt.close(fig)


def timeline(recs, out: Path):
    picks = {}
    for rec in sorted(recs, key=lambda r: (r.get("rep", 0), r["name"])):
        if rec.get("exp") == "partition" and rec.get("status") == "ok":
            picks.setdefault((rec["members"], rec["fault"]), rec)
    if not picks:
        return
    keys = sorted(picks)
    fig, axes = plt.subplots(len(keys), 1, figsize=(8, 1.6 * len(keys) + 0.6), sharex=True, squeeze=False)
    for ax, k in zip(axes[:, 0], keys):
        rec = picks[k]
        tf, th = rec["actions"]["fault"]["t_done"], rec["actions"]["heal"]["t_done"]
        ax.axvspan(tf, th, color=GRID, zorder=0)
        for y, (p, label) in enumerate((("maj", "majority side"), ("min", "minority side"))):
            ws = rec["writes"][p]
            # Acks at the time they arrived (a write sent just before the heal can be acked after
            # it); failures at the time they were sent.
            ok = [w[2] for w in ws if w[3] == "ok"]
            bad = [w[1] for w in ws if w[3] != "ok"]
            ax.scatter(bad, [y - 0.12] * len(bad), s=6, color=FAIL_C, marker="|",
                       label="write failed / timed out (at send time)" if y == 0 else None)
            ax.scatter(ok, [y + 0.12] * len(ok), s=6, color=OK_C, marker="|",
                       label="ack arrived" if y == 0 else None)
        ax.set_yticks([0, 1], ["majority", "minority"])
        ax.set_ylim(-0.6, 1.6)
        ax.grid(False)
        old = "old leader in minority" if k[1] == "leader-minority" else "old leader in majority"
        ax.set_title(f"{k[0]} members, {old} (trial {rec['name']})", color=INK, fontsize=8, loc="left")
    axes[0, 0].legend(frameon=False, fontsize=7, loc="upper right", ncol=2)
    axes[-1, 0].set_xlabel("time since the client started (s); shaded: partitioned")
    fig.tight_layout()
    fig.savefig(out / "partition_timeline.png", dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    recs = load(a.dirs)
    rows = [r for rec in recs for r in recompute(rec)]
    sweep(rows, "delay_ms", "loss_pct", "one-way delay between members (ms), no loss", "election_vs_delay.png", a.out)
    sweep(rows, "loss_pct", "delay_ms", "packet loss per direction (%), no added delay", "election_vs_loss.png", a.out)
    heatmaps(rows, a.out)
    timeline(recs, a.out)
    print("wrote", sorted(p.name for p in a.out.glob("*.png")))


if __name__ == "__main__":
    main()
