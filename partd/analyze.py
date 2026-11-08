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


USABLE = ("ok", "readback-failed")


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
    if rec.get("status") not in USABLE:
        return [base]
    tf = rec["actions"]["fault"]["t_done"]
    win = rec["window_s"]
    el = metrics.election_after(rec["events"], tf, rec["leader_before"]["id"])
    rb = rec.get("readback", {})
    base.update(
        election_s=el["election_s"], all_know_s=el["all_know_s"], candidate_rounds=el["candidate_rounds"],
        pre_candidate_rounds=el["pre_candidate_rounds"],
        leader_changes_before_fault=metrics.leader_changes(rec["events"], 0, tf),
        # more than 1: a new leader lost leadership again inside the window
        leaders_in_window=metrics.leader_changes(rec["events"], tf, tf + win),
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
        # Baseline: writes that finished (or timed out) before the fault could touch them. Writes
        # sent in the last client-timeout before the fault can still be in flight when it hits;
        # in run 42452414571 those were acked ~1.7 s later by the new leader, which made the
        # "before" p99 look like an election.
        before = metrics.write_stats(ws, 0, tf - rec["client_timeout_s"])
        fault = metrics.write_stats(ws, tf, tf + win)
        allw = metrics.write_stats(ws)
        r.update(
            before_p50_ms=before.get("p50_ms"), before_p99_ms=before.get("p99_ms"),
            baseline_writes=before.get("sent"),
            fault_success=fault.get("success_rate"), fault_sent=fault.get("sent"), fault_acked=fault.get("acked"),
            longest_gap_s=allw.get("longest_gap_s"), all_success=allw.get("success_rate"),
            all_p50_ms=allw.get("p50_ms"), all_p99_ms=allw.get("p99_ms"), all_max_ms=allw.get("max_ms"),
            first_ack_after_fault_s=metrics.first_ack_after(ws, tf),
        )
        if "heal" in rec["actions"]:
            th = rec["actions"]["heal"]["t_done"]
            r["first_ack_after_heal_s"] = metrics.first_ack_after(ws, th)
            r["after_heal_success"] = metrics.write_stats(ws, th).get("success_rate")
            # [fault applied, heal started): the cut is complete only in there. The heal flushes
            # one namespace at a time, and in run 42452414571 queued minority writes were acked
            # between the first and the last flush.
            hs = rec["actions"]["heal"]["t_start"]
            r["acks_during_fault"] = metrics.acks_completed(ws, tf, hs)
            r["fault_window_s"] = round(hs - tf, 6)
            r["heal_apply_s"] = round(th - hs, 6)
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
        # A trial whose read-back failed still has a valid election and write record (in run
        # 42452414571 the cluster had no stable leader for the read), so it stays in the medians
        # and is counted in readback_failed; its keys can't be checked, so it adds to neither
        # 'acked' nor 'lost'.
        ok = [r for r in g if r.get("status") in USABLE]
        row = dict(zip(keys, k))
        row["trials"] = len(g)
        row["ok"] = len(ok)
        row["readback_failed"] = sum(1 for r in g if r.get("status") == "readback-failed")
        row["not_ok"] = ";".join(sorted({r["status"] for r in g if r.get("status") not in USABLE}))
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
                    ("leaders_in_window", "leaders_in_window"), ("baseline_writes", "baseline_writes"),
                    ("fault_success", "fault_success"), ("longest_gap_s", "longest_gap_s"),
                    ("before_p50_ms", "before_p50_ms"), ("before_p99_ms", "before_p99_ms"),
                    ("all_p50_ms", "all_p50_ms"), ("all_p99_ms", "all_p99_ms")])
    write_csv(a.out / "election_summary.csv", el)
    pa = summarise([r for r in rows if r["exp"] == "partition"],
                   ("members", "timeouts", "delay_ms", "fault", "side"),
                   [("election_s", "election_s"), ("acks_during_fault", "acks_during_fault"),
                    ("fault_success", "fault_success"),
                    ("longest_gap_s", "longest_gap_s"), ("first_ack_after_fault_s", "first_ack_after_fault_s"),
                    ("first_ack_after_heal_s", "first_ack_after_heal_s"), ("all_p50_ms", "all_p50_ms"),
                    ("all_p99_ms", "all_p99_ms")])
    write_csv(a.out / "partition_summary.csv", pa)
    print(f"{len(rows)} rows; election groups {len(el)}, partition groups {len(pa)}")
    for r in el + pa:
        print({k: v for k, v in r.items() if v not in ("", None)})


if __name__ == "__main__":
    main()
