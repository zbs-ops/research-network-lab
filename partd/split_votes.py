"""Part D: in repeated split votes, do the two surviving members draw the same election timeouts?

etcd's raft re-draws a random election timeout every time a member becomes a candidate. For each
3-member election trial that needed more than one election, this takes each survivor's interval
from becoming candidate to starting its next election, pairs the two survivors' intervals round
by round, and counts the rounds where they agree within 5 ms. With independent draws over the 10
possible tick counts, about 1 round in 10 would agree by chance.

  python3 partd/split_votes.py results/partd > results/partd/split_votes.csv
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def intervals(events: list[dict], member: int, t_from: float, t_to: float) -> list[float]:
    out, last = [], None
    for e in events:
        if e.get("m") != member or not t_from <= e["t"] <= t_to:
            continue
        if e["kind"] == "candidate":
            last = e["t"]
        elif e["kind"] == "election" and last is not None:
            out.append(e["t"] - last)
            last = None
    return out


def main() -> None:
    rows = []
    for f in sorted(Path(sys.argv[1]).rglob("election_n3_*.json")):
        d = json.loads(f.read_text())
        if d.get("status") not in ("ok", "readback-failed"):
            continue
        el = d["election"]
        if not el.get("election_s") or el["candidate_rounds"] < 2:
            continue
        tf = d["actions"]["fault"]["t_done"]
        end = tf + el["election_s"]
        old = d["leader_before"]["member"]
        a, b = [m for m in (1, 2, 3) if m != old]
        ia, ib = intervals(d["events"], a, tf, end), intervals(d["events"], b, tf, end)
        pairs = list(zip(ia, ib))
        rows.append({
            "trial": d["name"], "run": next((p[4:] for p in f.parts if p.startswith("run-")), "?"),
            "timeouts": d["timeouts"], "delay_ms": d["delay_ms"], "loss_pct": d["loss_pct"],
            "candidate_rounds": el["candidate_rounds"], "paired_rounds": len(pairs),
            "same_within_5ms": sum(1 for x, y in pairs if abs(x - y) < 0.005),
        })
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    tot = sum(r["paired_rounds"] for r in rows)
    same = sum(r["same_within_5ms"] for r in rows)
    print(f"# {len(rows)} trials, {same} of {tot} paired rounds the same within 5 ms", file=sys.stderr)


if __name__ == "__main__":
    main()
