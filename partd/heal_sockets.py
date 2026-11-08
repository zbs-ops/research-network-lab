"""Part D: does TCP backoff set how long a partitioned member takes to recover after a heal?

For each partition trial with socket snapshots (recorded by raft_faults.py just before the heal),
find the connection from the current leader to each minority member with the most unsent data
(Send-Q), read how long its backed-off timer still had to run (ss: timer:(persist|on,<left>,n)),
and compare when that timer fires with when the minority writer got its first ack after the heal.

With --logs (the run's partd-logs-* artifact, unpacked), also reports when the leader's etcd
logged its first "lost TCP streaming connection" after the heal: etcd giving up on a stuck
stream and dialling a new one is the other way out.

  python3 partd/heal_sockets.py results/partd/run-<id> [--logs <dir>] > results/partd/run-<id>/heal_sockets.csv
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from metrics import parse_ts

TIMER = re.compile(r"timer:\((on|persist|keepalive),([0-9.]+)(ms|sec|min)?,(\d+)\)")


def seconds(val: str, unit: str | None) -> float:
    # ss prints the time left as e.g. "200ms", "3.024ms" (meaning 3 s 24 ms: ss prints seconds
    # under 10 s as <s>.<ms>ms) or "10sec". A value with a dot is seconds.milliseconds: in run
    # 42454644849 a keepalive timer read "10sec" and then "9.047ms" 1.15 s later.
    if "." in val:
        return float(val)
    if unit == "ms":
        return int(val) / 1000
    if unit == "min":
        return int(val) * 60
    return float(val)


def stream_lost_after(logdir: Path | None, d: dict, member: int, t_from: float) -> float | None:
    """Seconds from t_from to member's first 'lost TCP streaming connection' log line."""
    if logdir is None:
        return None
    f = next(logdir.rglob(f"{d['name']}/m{member}.log"), None)
    if f is None:
        return None
    for line in f.read_text(errors="replace").splitlines():
        if "lost TCP streaming connection" in line:
            t = parse_ts(json.loads(line)["ts"]) - d["t0_epoch"]
            if t > t_from:
                return round(t - t_from, 3)
    return None


def main() -> None:
    args = sys.argv[1:]
    logdir = None
    if "--logs" in args:
        i = args.index("--logs")
        logdir = Path(args[i + 1])
        del args[i:i + 2]
    rows = []
    for f in sorted(Path(args[0]).rglob("partition_*.json")):
        d = json.loads(f.read_text())
        a = d.get("actions", {})
        if d.get("status") != "ok" or "sockets_before_heal" not in a:
            continue
        leader_m = d["after"]["leader"] if d.get("after") else None
        snap_t, heal_t = a["sockets_before_heal"]["t_start"], a["heal"]["t_start"]
        socks = a["sockets_before_heal"]["applied"].get(str(leader_m), [])
        for m in d["minority"]:
            best = None
            for line in socks:
                parts = line.split()
                if len(parts) < 4 or not parts[3].startswith(f"10.20.0.{m}:"):
                    continue
                t = TIMER.search(line)
                if t and t.group(1) in ("on", "persist") and (best is None or int(parts[1]) > best[0]):
                    best = (int(parts[1]), t.group(1), seconds(t.group(2), t.group(3)), int(t.group(4)))
            if best is None:
                continue
            fires = round(best[2] - (heal_t - snap_t), 3)  # seconds after the heal started
            rows.append({
                "trial": d["name"], "delay_ms": d["delay_ms"], "leader": leader_m, "minority_member": m,
                "send_q_bytes": best[0], "timer": best[1], "backoff": best[3],
                "timer_fires_after_heal_s": fires,
                "leader_stream_lost_after_heal_s": stream_lost_after(logdir, d, leader_m, heal_t),
                "first_minority_ack_after_heal_s": d["stats"]["min"]["first_ack_after_heal_s"],
            })
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else ["trial"])
    w.writeheader()
    w.writerows(rows)


if __name__ == "__main__":
    main()
