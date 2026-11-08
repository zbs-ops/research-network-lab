"""Part D: Raft (etcd) under network faults.

Two experiments, each trial on a fresh etcd cluster, one member per network namespace:

  election   The leader is killed (SIGKILL) or isolated (iptables), while one-way delay and
             loss between members are set with netem. A client writes at a fixed rate to a
             follower that stays healthy. Measures: time to a new leader (from etcd's raft log),
             write success rate, longest stretch with no acked write, write latency, and a
             linearizable read-back of every acknowledged key.
  partition  The cluster is split into a minority and a majority, with the old leader on one
             side or the other. Two clients write at once, one to a member on each side. Shows
             which side keeps accepting writes, then heals and reads every acked key back.

Each trial writes one JSON file with its raw per-write record, the raft events, what was applied
to the network, and the metrics computed from them (analyze.py recomputes them from the raw data).

Run as root on Linux, with net.py's namespaces:
  python3 partd/raft_faults.py --exp election --members 3 --faults kill --out /tmp/partd
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import platform
import time
from collections import Counter
from pathlib import Path

import client
import metrics
import net
from cluster import Cluster, etcd_version, fresh_token

TIMEOUTS = {"default": (100, 1000), "tuned": (50, 500)}  # (heartbeat ms, election timeout ms)


def floats(s: str) -> list[float]:
    return [float(x) if "." in x else int(x) for x in s.split(",") if x != ""]


def grid(delays, losses, mode):
    if mode == "cross":
        return list(itertools.product(delays, losses))
    # axes: delay sweep with no loss, plus loss sweep with no delay
    return [(d, 0) for d in delays] + [(0, lo) for lo in losses if lo != 0]


def warn_summary(c: Cluster) -> dict:
    """Counts of etcd warning messages across members (e.g. late heartbeats)."""
    cnt = Counter()
    for line in c.log_lines('"level":"warn"'):
        try:
            cnt[json.loads(line.split(": ", 1)[1]).get("msg", "?")] += 1
        except Exception:
            cnt["(unparsed)"] += 1
    return dict(cnt.most_common(12))


def readback(c: Cluster, url_member: int, prefixes: list[str], deadline_s: float = 15) -> tuple[dict, str | None]:
    t_end = time.time() + deadline_s
    err = None
    while time.time() < t_end:
        try:
            out = {}
            for p in prefixes:
                out.update(client.read_prefix(c.url(url_member), p))
            return out, None
        except Exception as e:  # leader may still be settling after a heal
            err = repr(e)[:200]
            time.sleep(0.3)
    return {}, err


async def run_trial_async(c, streams, actions, t0):
    """streams: [(member, prefix)]; actions: [(t_rel, name, fn)] run at their times."""
    rec = {}

    async def act():
        for t_rel, name, fn in actions:
            await asyncio.sleep(max(0, t0 + t_rel - time.time()))
            start = time.time()
            out = await asyncio.to_thread(fn)
            rec[name] = {"t_start": round(start - t0, 6), "t_done": round(time.time() - t0, 6), "applied": out}

    res = await asyncio.gather(act(), *(client.write_stream(c.client_ip(m), p, ARGS.rate, ARGS.duration,
                                                            ARGS.client_timeout, t0) for m, p in streams))
    return rec, res[1:]


def trial(exp: str, n: int, tmo: str, fault: str, delay, loss, rep: int, out: Path, local: bool) -> dict:
    hb, el = TIMEOUTS[tmo]
    name = f"{exp}_n{n}_{tmo}_{fault}_d{delay}_l{loss}_r{rep}"
    rec = {"name": name, "exp": exp, "members": n, "timeouts": tmo, "heartbeat_ms": hb, "election_ms": el,
           "fault": fault, "delay_ms": delay, "loss_pct": loss, "rep": rep, "rate": ARGS.rate,
           "client_timeout_s": ARGS.client_timeout, "pre_s": ARGS.pre, "window_s": ARGS.window,
           "duration_s": ARGS.duration, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not local:
        rec["qdisc"] = net.shape(n, delay, loss)
        net.heal(n)
    c = Cluster(n, ARGS.etcd, out / "logs" / name, hb, el, fresh_token(name), local)
    t_launch = time.time()
    c.start()
    try:
        ready = c.wait_ready(ARGS.form_timeout)
        if ready is None:
            rec["status"] = "no-leader-at-start"
            return rec
        time.sleep(1.0)
        ready = c.wait_ready(10)
        if ready is None:
            rec["status"] = "leader-lost-before-fault"
            return rec
        L = ready["leader"]
        followers = [i for i in range(1, n + 1) if i != L]
        rec["formed_s"] = round(time.time() - t_launch, 3)
        rec["leader_before"] = {"member": L, "id": c.ids[L], "term": ready["term"]}
        rec["ids"] = c.ids
        t_fault = ARGS.pre
        actions = []

        if exp == "election":
            target = followers[0]
            streams = [(target, "w")]
            rec["write_target"] = target
            if fault == "kill":
                actions.append((t_fault, "fault", lambda: (c.kill(L), f"SIGKILL m{L}")[1]))
            else:
                actions.append((t_fault, "fault", lambda: net.isolate(n, L)))
                actions.append((t_fault + ARGS.window, "heal", lambda: (net.heal(n), "flushed")[1]))
        else:  # partition
            m = (n - 1) // 2
            if fault == "leader-minority":
                minority = [L] + followers[: m - 1]
            else:
                minority = followers[:m]
            majority = [i for i in range(1, n + 1) if i not in minority]
            min_target = L if L in minority else minority[0]
            maj_target = next(i for i in majority if i != L)
            streams = [(min_target, "min"), (maj_target, "maj")]
            rec.update(minority=minority, majority=majority, write_targets={"min": min_target, "maj": maj_target})
            actions.append((t_fault, "fault", lambda: net.partition(n, minority, majority)))
            actions.append((t_fault + ARGS.window, "heal", lambda: (net.heal(n), "flushed")[1]))

        t0 = time.time() + 0.2
        rec["t0_epoch"] = t0
        acts, results = asyncio.run(run_trial_async(c, streams, actions, t0))
        rec["actions"] = acts
        rec["writes"] = {p: r[0] for (_, p), r in zip(streams, results)}

        after = c.wait_ready(15)
        rec["after"] = after
        rec["terms_after"] = {i: (s or {}).get("raftTerm") for i in c.alive() for s in [c.status(i)]}
        values = {p: r[1] for (_, p), r in zip(streams, results)}
        acked = {k: v for p, ws in rec["writes"].items() for (seq, _, _, o), (k, v) in zip(ws, values[p].items()) if o == "ok"}
        unacked = {k: v for p, ws in rec["writes"].items() for (seq, _, _, o), (k, v) in zip(ws, values[p].items()) if o != "ok"}
        read_from = streams[-1][0] if exp == "partition" else streams[0][0]
        present, err = readback(c, read_from, [p for _, p in streams])
        rec["readback"] = metrics.check_readback(acked, unacked, present) if err is None else {"error": err}
        rec["readback_from"] = read_from
        rec["status"] = "ok" if err is None else "readback-failed"
    finally:
        c.stop()
        rec["events"] = c.events(rec.get("t0_epoch", t_launch))
        rec["etcd_warnings"] = warn_summary(c)
        if not local:
            net.heal(n)

    # Metrics from the raw record (analyze.py recomputes these the same way).
    tf = rec["actions"]["fault"]["t_done"]
    rec["t_fault"] = tf
    rec["fault_apply_s"] = round(rec["actions"]["fault"]["t_done"] - rec["actions"]["fault"]["t_start"], 6)
    rec["election"] = metrics.election_after(rec["events"], tf, rec["leader_before"]["id"])
    rec["leader_changes_before_fault"] = metrics.leader_changes(rec["events"], 0, tf)
    rec["stats"] = {}
    for p, ws in rec["writes"].items():
        rec["stats"][p] = {
            "before": metrics.write_stats(ws, 0, tf),
            "fault": metrics.write_stats(ws, tf, tf + ARGS.window),
            "all": metrics.write_stats(ws),
            "first_ack_after_fault_s": metrics.first_ack_after(ws, tf),
        }
        if "heal" in rec["actions"]:
            th = rec["actions"]["heal"]["t_done"]
            rec["stats"][p]["after_heal"] = metrics.write_stats(ws, th)
            rec["stats"][p]["first_ack_after_heal_s"] = metrics.first_ack_after(ws, th)
    return rec


def main() -> None:
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", choices=["election", "partition"], required=True)
    ap.add_argument("--members", type=int, default=3)
    ap.add_argument("--timeouts", default="default", help="comma list of: " + ",".join(TIMEOUTS))
    ap.add_argument("--faults", default="kill,isolate", help="election: kill,isolate; partition: leader-minority,leader-majority")
    ap.add_argument("--delays", default="0", help="one-way delay between members, ms")
    ap.add_argument("--losses", default="0", help="loss per direction between members, %%")
    ap.add_argument("--grid", choices=["cross", "axes"], default="cross")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--rate", type=float, default=50, help="writes per second, per client")
    ap.add_argument("--client-timeout", type=float, default=2.0)
    ap.add_argument("--pre", type=float, default=3.0, help="seconds of writes before the fault")
    ap.add_argument("--window", type=float, default=8.0, help="seconds from the fault to the heal")
    ap.add_argument("--post", type=float, default=4.0, help="seconds of writes after the window")
    ap.add_argument("--form-timeout", type=float, default=60.0)
    ap.add_argument("--etcd", default="/usr/local/bin/etcd")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--local", action="store_true", help="no namespaces, kill only (development)")
    ARGS = ap.parse_args()
    ARGS.duration = ARGS.pre + ARGS.window + ARGS.post
    ARGS.out.mkdir(parents=True, exist_ok=True)

    tmos, faults = ARGS.timeouts.split(","), ARGS.faults.split(",")
    cells = grid(floats(ARGS.delays), floats(ARGS.losses), ARGS.grid)
    manifest = {"argv": vars(ARGS) | {"out": str(ARGS.out), "duration": ARGS.duration},
                "etcd": etcd_version(ARGS.etcd), "kernel": platform.release(), "cells": cells,
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not ARGS.local:
        manifest["net_setup"] = net.setup(ARGS.members)
    print(json.dumps(manifest, indent=1), flush=True)
    done = Counter()
    try:
        # Repetition is the outer loop, so slow drift on the runner spreads over every config.
        for rep in range(1, ARGS.reps + 1):
            for tmo, fault, (d, lo) in itertools.product(tmos, faults, cells):
                try:
                    rec = trial(ARGS.exp, ARGS.members, tmo, fault, d, lo, rep, ARGS.out, ARGS.local)
                except Exception as ex:  # keep going; the failure is recorded, not hidden
                    import traceback
                    rec = {"name": f"{ARGS.exp}_n{ARGS.members}_{tmo}_{fault}_d{d}_l{lo}_r{rep}",
                           "status": "error", "error": traceback.format_exc()[-2000:]}
                    print(rec["error"], flush=True)
                (ARGS.out / f"{rec['name']}.json").write_text(json.dumps(rec, separators=(",", ":")))
                done[rec.get("status")] += 1
                e = rec.get("election", {})
                s = rec.get("stats", {})
                print(f"{rec['name']}: {rec.get('status')} election={e.get('election_s')} "
                      f"rounds={e.get('candidate_rounds')} "
                      + " ".join(f"{p}:ok={v['fault'].get('success_rate')},gap={v['fault'].get('longest_gap_s')}"
                                 for p, v in s.items())
                      + f" lost={rec.get('readback', {}).get('lost')}", flush=True)
    finally:
        manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest["status_counts"] = dict(done)
        (ARGS.out / f"manifest_{ARGS.exp}_n{ARGS.members}_{ARGS.timeouts}_{ARGS.faults}.json".replace(",", "+")
         ).write_text(json.dumps(manifest, indent=1))
        if not ARGS.local:
            net.teardown()


ARGS: argparse.Namespace

if __name__ == "__main__":
    main()
