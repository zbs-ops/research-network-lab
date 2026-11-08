"""Part D metrics: pure functions over a trial's raw record, shared by the harness and analyze.py.

Times are seconds. Inside a trial record they are relative to the trial's t0 (the epoch time
the client started writing); etcd's log timestamps are converted to the same scale.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)$")

# Raft messages etcd 3.5 logs through zap (logger "raft"). Checked against etcd v3.5.35's output.
_EVENTS = [
    ("election", re.compile(r"^([0-9a-f]+) is starting a new election at term (\d+)$")),
    ("pre-candidate", re.compile(r"^([0-9a-f]+) became pre-candidate at term (\d+)$")),
    ("candidate", re.compile(r"^([0-9a-f]+) became candidate at term (\d+)$")),
    ("leader", re.compile(r"^([0-9a-f]+) became leader at term (\d+)$")),
    ("follower", re.compile(r"^([0-9a-f]+) became follower at term (\d+)$")),
    ("elected", re.compile(r"^raft\.node: ([0-9a-f]+) elected leader ([0-9a-f]+) at term (\d+)$")),
    ("lost", re.compile(r"^raft\.node: ([0-9a-f]+) lost leader ([0-9a-f]+) at term (\d+)$")),
]


def parse_ts(ts: str) -> float:
    """etcd's zap timestamp ('2026-11-08T05:43:29.603093Z') -> epoch seconds."""
    m = _TS.match(ts)
    if not m:
        raise ValueError(f"unrecognised timestamp {ts!r}")
    base, frac, tz = m.groups()
    dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    if tz == "Z":
        off = 0
    else:
        sign = 1 if tz[0] == "+" else -1
        hh, mm = int(tz[1:3]), int(tz[-2:])
        off = sign * (hh * 3600 + mm * 60)
    return dt.replace(tzinfo=timezone.utc).timestamp() - off + (float(frac) if frac else 0.0)


def parse_raft_events(lines, t0: float = 0.0) -> list[dict]:
    """Raft state changes from one member's etcd log lines, times relative to t0."""
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{") or '"raft"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("logger") != "raft":
            continue
        msg = d.get("msg", "")
        for kind, rx in _EVENTS:
            m = rx.match(msg)
            if not m:
                continue
            ev = {"t": round(parse_ts(d["ts"]) - t0, 6), "kind": kind, "member": m.group(1), "term": int(m.groups()[-1])}
            if kind in ("elected", "lost"):
                ev["leader"] = m.group(2)
            out.append(ev)
            break
    return out


def election_after(events: list[dict], t_fault: float, old_leader: str) -> dict:
    """How the cluster replaced its leader after a fault at t_fault.

    election_s: fault -> first 'became leader' by a member other than the old leader.
    all_know_s: fault -> the last member that ends up following the new leader logged it
    (members that never log it, e.g. a killed or still-isolated one, are left out).
    candidate_rounds: 'became candidate' events (real elections, after pre-vote) between the
    fault and the new leader; 1 means the first election won.
    """
    evs = sorted((e for e in events if e["t"] >= t_fault), key=lambda e: e["t"])
    new = next((e for e in evs if e["kind"] == "leader" and e["member"] != old_leader), None)
    res = {"election_s": None, "new_leader": None, "new_term": None, "all_know_s": None,
           "candidate_rounds": sum(1 for e in evs if e["kind"] == "candidate"),
           "pre_candidate_rounds": sum(1 for e in evs if e["kind"] == "pre-candidate")}
    if new is None:
        return res
    res.update(election_s=round(new["t"] - t_fault, 6), new_leader=new["member"], new_term=new["term"])
    res["candidate_rounds"] = sum(1 for e in evs if e["kind"] == "candidate" and e["t"] <= new["t"])
    res["pre_candidate_rounds"] = sum(1 for e in evs if e["kind"] == "pre-candidate" and e["t"] <= new["t"])
    know = [e["t"] for e in evs if e["kind"] == "elected" and e["leader"] == new["member"] and e["term"] == new["term"]]
    if know:
        res["all_know_s"] = round(max(know) - t_fault, 6)
    return res


def leader_changes(events: list[dict], t_from: float, t_to: float) -> int:
    """'became leader' events in [t_from, t_to): leader churn with no fault injected."""
    return sum(1 for e in events if e["kind"] == "leader" and t_from <= e["t"] < t_to)


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def write_stats(writes: list[list], t_from: float | None = None, t_to: float | None = None) -> dict:
    """Stats for writes *sent* in [t_from, t_to). Each write is [seq, t_send, t_done, outcome].

    longest_gap_s: longest time with no acknowledged write, measured between consecutive ack
    times, with the window's start (first send) and end (last send) as the outer bounds. With
    no fault it is about one sending interval.
    """
    sel = [w for w in writes if (t_from is None or w[1] >= t_from) and (t_to is None or w[1] < t_to)]
    if not sel:
        return {"sent": 0}
    ok = [w for w in sel if w[3] == "ok"]
    lat = [(w[2] - w[1]) * 1000 for w in ok]
    start, end = min(w[1] for w in sel), max(w[1] for w in sel)
    marks = [start] + sorted(w[2] for w in ok if start <= w[2] <= end) + [end]
    gaps = [b - a for a, b in zip(marks, marks[1:])]
    outcomes: dict[str, int] = {}
    for w in sel:
        outcomes[w[3]] = outcomes.get(w[3], 0) + 1
    return {
        "sent": len(sel),
        "acked": len(ok),
        "success_rate": round(len(ok) / len(sel), 6),
        "longest_gap_s": round(max(gaps), 6) if gaps else None,
        "p50_ms": round(percentile(lat, 50), 3) if lat else None,
        "p99_ms": round(percentile(lat, 99), 3) if lat else None,
        "max_ms": round(max(lat), 3) if lat else None,
        "outcomes": outcomes,
    }


def acks_completed(writes: list[list], t_from: float, t_to: float) -> int:
    """Acks that *arrived* in [t_from, t_to), whenever the write was sent.

    write_stats groups writes by send time, so a write sent just before a heal and acknowledged
    just after it counts as a success there. To ask whether a side accepted writes while it was
    partitioned, count the acks that arrived during the partition instead.
    """
    return sum(1 for w in writes if w[3] == "ok" and t_from <= w[2] < t_to)


def first_ack_after(writes: list[list], t: float) -> float | None:
    """Delay from t to the first ack of a write sent at or after t."""
    acks = [w[2] for w in writes if w[3] == "ok" and w[1] >= t]
    return round(min(acks) - t, 6) if acks else None


def check_readback(acked: dict[str, str], unacked: dict[str, str], present: dict[str, str]) -> dict:
    """Compare a linearizable read of every key against what the client wrote.

    acked/unacked: key -> value the client sent. present: key -> value the cluster returned.
    lost: acknowledged but missing; wrong: acknowledged but a different value. Both must be 0.
    unacked_committed: writes that failed or timed out at the client yet were committed (legal:
    a timeout says nothing about the outcome).
    """
    lost = sorted(k for k in acked if k not in present)
    wrong = sorted(k for k in acked if k in present and present[k] != acked[k])
    phantom = sorted(k for k in present if k not in acked and k not in unacked)
    return {
        "acked": len(acked),
        "lost": len(lost),
        "wrong": len(wrong),
        "unacked": len(unacked),
        "unacked_committed": sum(1 for k in unacked if k in present),
        "unknown_keys": len(phantom),
        "lost_keys": lost[:20],
        "wrong_keys": wrong[:20],
    }
