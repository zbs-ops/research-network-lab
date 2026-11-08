"""Part D: summary tables from the committed trial files.

Recomputes every metric from each trial's raw record (raft events and per-write outcomes) with
partd/metrics.py, so the summaries depend only on committed data. Every row lists the CI runs
its trials came from.

  python3 partd/analyze.py results/partd [more dirs ...] --out results/partd

Writes election_summary.csv, partition_summary.csv and trials.csv (one row per trial).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

import metrics


def run_id(path: Path) -> str:
    m = re.search(r"run-(\d+)", str(path))
    return m.group(1) if m else "?"


def load(dirs: list[Path]) -> list[dict]:
    out = []
    for d in dirs:
        for f in sorted(d.rglob("*.json")):
            if f.name.startswith(("manifest", "summary")) or not f.name.startswith(("election_", "partition_")):
                continue
            rec = json.loads(f.read_text())
            rec["_run"] = run_id(f)
            rec["_file"] = str(f)
            out.append(rec)
    return out


def recompute(rec: dict) -> dict:
    """One flat row per trial (per write stream for partition trials)."""
    base = {k: rec.get(k) for k in ("exp", "members", "timeouts", "heartbeat_ms", "election_ms", "fault",
                                    "delay_ms", "loss_pct", "rep", "status")}
    base["run"] = rec["_run"]
    if rec.get("status") not in ("ok", "readback-failed"):
        return [base]
    tf = rec["actions"]["fault"]["t_done"]
    win = rec["window_s"]
    el = metrics.election_after(rec["events"], tf, rec["leader_before"]["id"])
    rb = rec.get("readback", {})
    base.update(
        election_s=el["election_s"], all_know_s=el["all_know_s"], candidate_rounds=el["candidate_rounds"],
        pre_candidate_rounds=el["pre_candidate_rounds"],
        leader_changes_before_fault=metrics.leader_changes(rec["events"], 0, tf),
        fault_apply_s=round(tf - rec["actions"]["fault"]["t_start"], 6),
        acked=rb.get("acked"), lost=rb.get("lost"), wrong=rb.get("wrong"),
        unacked_committed=rb.get("unacked_committed"), unacked=rb.get("unacked"),
        readback_error=rb.get("error"),
    )
    rows = []
    for p, ws in rec["writes"].items():
        r = dict(base, stream=p)
        if rec["exp"] == "partition":
            r["side"] = {"min": "minority", "maj": "majority"}[p]
        before = metrics.write_stats(ws, 0, tf)
        fault = metrics.write_stats(ws, tf, tf + win)
        allw = metrics.write_stats(ws)
        r.update(
            before_p50_ms=before.get("p50_ms"), before_p99_ms=before.get("p99_ms"),
            fault_success=fault.get("success_rate"), fault_sent=fault.get("sent"), fault_acked=fault.get("acked"),
            longest_gap_s=allw.get("longest_gap_s"), all_success=allw.get("success_rate"),
            all_p50_ms=allw.get("p50_ms"), all_p99_ms=allw.get("p99_ms"), all_max_ms=allw.get("max_ms"),
            first_ack_after_fault_s=metrics.first_ack_after(ws, tf),
        )
        if "heal" in rec["actions"]:
            th = rec["actions"]["heal"]["t_done"]
            r["first_ack_after_heal_s"] = metrics.first_ack_after(ws, th)
            r["after_heal_success"] = metrics.write_stats(ws, th).get("success_rate")
        rows.append(r)
    return rows


def med_range(vals) -> str:
    v = [x for x in vals if x is not None]
    if not v:
        return ""
    f = (lambda x: f"{x:.3g}") if max(abs(x) for x in v) < 10 else (lambda x: f"{x:.0f}")
    return f"{f(statistics.median(v))} [{f(min(v))}-{f(max(v))}]"


def summarise(rows: list[dict], keys: tuple, cols: list[tuple]) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(k) for k in keys)].append(r)
    out = []
    for k in sorted(groups, key=lambda t: tuple((x is None, x) for x in t)):
        g = groups[k]
        ok = [r for r in g if r.get("status") == "ok"]
        row = dict(zip(keys, k))
        row["trials"] = len(g)
        row["ok"] = len(ok)
        row["not_ok"] = ";".join(sorted({r["status"] for r in g if r.get("status") != "ok"}))
        for name, field in cols:
            vals = [r.get(field) for r in ok]
            row[name] = med_range(vals)
            if field == "election_s":
                row["no_new_leader"] = sum(1 for x in vals if x is None)
        row["lost"] = sum(r.get("lost") or 0 for r in ok)
        row["wrong"] = sum(r.get("wrong") or 0 for r in ok)
        row["acked"] = sum(r.get("acked") or 0 for r in ok)
        row["unacked_committed"] = sum(r.get("unacked_committed") or 0 for r in ok)
        row["runs"] = " ".join(sorted({r["run"] for r in g}))
        out.append(row)
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = [r for rec in load(a.dirs) for r in recompute(rec)]
    write_csv(a.out / "trials.csv", rows)
    el = summarise([r for r in rows if r["exp"] == "election"],
                   ("members", "timeouts", "fault", "delay_ms", "loss_pct"),
                   [("election_s", "election_s"), ("candidate_rounds", "candidate_rounds"),
                    ("leader_changes_before_fault", "leader_changes_before_fault"),
                    ("fault_success", "fault_success"), ("longest_gap_s", "longest_gap_s"),
                    ("before_p50_ms", "before_p50_ms"), ("before_p99_ms", "before_p99_ms"),
                    ("all_p50_ms", "all_p50_ms"), ("all_p99_ms", "all_p99_ms")])
    write_csv(a.out / "election_summary.csv", el)
    pa = summarise([r for r in rows if r["exp"] == "partition"],
                   ("members", "timeouts", "fault", "side"),
                   [("election_s", "election_s"), ("fault_success", "fault_success"),
                    ("longest_gap_s", "longest_gap_s"), ("first_ack_after_fault_s", "first_ack_after_fault_s"),
                    ("first_ack_after_heal_s", "first_ack_after_heal_s"), ("all_p50_ms", "all_p50_ms"),
                    ("all_p99_ms", "all_p99_ms")])
    write_csv(a.out / "partition_summary.csv", pa)
    print(f"{len(rows)} rows; election groups {len(el)}, partition groups {len(pa)}")
    for r in el + pa:
        print({k: v for k, v in r.items() if v not in ("", None)})


if __name__ == "__main__":
    main()
