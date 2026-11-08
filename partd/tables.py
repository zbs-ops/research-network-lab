"""Part D: the markdown tables in docs/part-d-raft.md, generated from the summary CSVs.

  python3 partd/tables.py results/partd > /tmp/tables.md

Nothing is typed by hand: every cell is a value from election_summary.csv or
partition_summary.csv (median [min-max] over the trials), and every row links the CI runs its
trials came from.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

RUN_URL = "https://github.com/zbs-ops/research-network-lab/actions/runs/"


def links(runs: str) -> str:
    return ", ".join(f"[{r}]({RUN_URL}{r})" for r in runs.split())


def table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def axes(rows):
    """Delay sweep at 0% loss, then loss sweep at 0 ms, as (label, delay, loss)."""
    delays = sorted({int(float(r["delay_ms"])) for r in rows})
    losses = sorted({int(float(r["loss_pct"])) for r in rows})
    return [(f"{d} ms, 0%", d, 0) for d in delays] + [(f"0 ms, {lo}%", 0, lo) for lo in losses if lo]


def pick(rows, **kw):
    for r in rows:
        if all(float(r[k]) == float(v) if k in ("delay_ms", "loss_pct", "members") else r[k] == v
               for k, v in kw.items()):
            return r
    return None


def cell(r, col, n=True):
    if r is None or not r.get(col):
        return "–"
    return r[col] + (f" ({r['ok']})" if n else "")


def election_tables(el):
    out = []
    for n in ("3", "5"):
        rows, runs = [], set()
        for label, d, lo in axes(el):
            line = [label]
            for fault in ("kill", "isolate"):
                for tmo in ("default", "tuned"):
                    r = pick(el, members=n, timeouts=tmo, fault=fault, delay_ms=d, loss_pct=lo)
                    line.append(cell(r, "election_s", n=False))
                    rr = pick(el, members=n, timeouts=tmo, fault=fault, delay_ms=d, loss_pct=lo)
                    if rr:
                        runs.update(rr["runs"].split())
            rounds = [pick(el, members=n, timeouts=t, fault="kill", delay_ms=d, loss_pct=lo) for t in ("default", "tuned")]
            line.append(" / ".join(cell(r, "candidate_rounds", n=False) for r in rounds))
            trials = {pick(el, members=n, timeouts=t, fault=f, delay_ms=d, loss_pct=lo)["ok"]
                      for t in ("default", "tuned") for f in ("kill", "isolate")
                      if pick(el, members=n, timeouts=t, fault=f, delay_ms=d, loss_pct=lo)}
            line.append("/".join(sorted(trials)))
            rows.append(line)
        out.append(f"**{n} members**, time to a new leader in seconds, median [min–max]. Runs: {links(' '.join(sorted(runs)))}\n")
        out.append(table(["one-way delay, loss", "killed, default", "killed, tuned", "isolated, default",
                          "isolated, tuned", "elections needed (killed: default / tuned)", "trials per cell"], rows))
        out.append("")
    return "\n".join(out)


def write_tables(el):
    out = []
    for n in ("3", "5"):
        rows, runs = [], set()
        for label, d, lo in axes(el):
            line = [label]
            for tmo in ("default", "tuned"):
                r = pick(el, members=n, timeouts=tmo, fault="kill", delay_ms=d, loss_pct=lo)
                line += [cell(r, "fault_success", n=False), cell(r, "longest_gap_s", n=False)]
                if r:
                    runs.update(r["runs"].split())
            r = pick(el, members=n, timeouts="default", fault="kill", delay_ms=d, loss_pct=lo)
            line += [cell(r, "before_p50_ms", n=False), cell(r, "before_p99_ms", n=False)]
            rows.append(line)
        out.append(f"**{n} members, leader killed.** Success: share of the writes sent in the 8 s after the kill that were acked. "
                   f"Gap: longest time with no ack, in seconds. Latency: writes before the fault, default timeouts, ms. "
                   f"Median [min–max] over trials. Runs: {links(' '.join(sorted(runs)))}\n")
        out.append(table(["one-way delay, loss", "success, default", "gap, default", "success, tuned", "gap, tuned",
                          "p50 latency", "p99 latency"], rows))
        out.append("")
    return "\n".join(out)


def partition_table(pa):
    rows = []
    for r in pa:
        delay = r.get("delay_ms", "0") or "0"
        rows.append([r["members"], f"{int(float(delay))} ms", "minority" if r["fault"] == "leader-minority" else "majority",
                     r["side"], r["ok"], cell(r, "acks_during_fault", n=False), cell(r, "longest_gap_s", n=False),
                     cell(r, "first_ack_after_heal_s", n=False), cell(r, "election_s", n=False), links(r["runs"])])
    return table(["members", "one-way delay", "old leader in", "writer on", "trials", "acks during the partition",
                  "longest gap (s)", "first ack after heal (s)", "new leader after (s)", "runs"], rows)


def safety_line(el, pa):
    acked = sum(int(r["acked"]) for r in el)
    lost = sum(int(r["lost"]) + int(r["wrong"]) for r in el)
    # partition rows repeat each trial's totals once per side; count each (members, fault, delay) once
    seen, packed, plost = set(), 0, 0
    for r in pa:
        k = (r["members"], r["fault"], r.get("delay_ms"))
        if k not in seen:
            seen.add(k)
            packed += int(r["acked"])
            plost += int(r["lost"]) + int(r["wrong"])
    trials = sum(int(r["ok"]) - int(r.get("readback_failed") or 0) for r in el)
    rbf = sum(int(r.get("readback_failed") or 0) for r in el)
    runs = sorted({x for r in el + pa for x in r["runs"].split()})
    return (f"Election trials with a completed read-back: {trials} ({rbf} read-back failed), {acked} acknowledged writes, "
            f"{lost} missing or wrong. Partition trials: {packed} acknowledged writes, {plost} missing or wrong. "
            f"Runs: {links(' '.join(runs))}")


def main() -> None:
    d = Path(sys.argv[1])
    el = list(csv.DictReader((d / "election_summary.csv").open()))
    pa = list(csv.DictReader((d / "partition_summary.csv").open()))
    print("## Election time\n")
    print(election_tables(el))
    print("## Write availability\n")
    print(write_tables(el))
    print("## Partitions\n")
    print(partition_table(pa))
    print("\n## Safety\n")
    print(safety_line(el, pa))


if __name__ == "__main__":
    main()
