"""Why do 4-stream BBR transfers at 250 ms sometimes stall completely?

Runs 41176590070 and 41178340217 showed it: in some repetitions all 4 streams stop making
progress around second 5, for the rest of the test, even at 0% loss with 0 retransmits, and
almost only at MTU 1500. This re-runs that configuration many times next to two controls
(same path at MTU 9000, and CUBIC with 4 streams at MTU 1500) and samples, every 0.5 s:

  - each flow's TCP state from `ss -tin` in the sender (cwnd, RTT, unacked, BBR's bandwidth
    estimate and pacing rate, retransmits),
  - both netem queues from `tc -s qdisc` (backlog, drops, requeues),
  - host CPU from /proc/stat (the emulator and both endpoints share one VM).

Run as root:  python3 partb/bbr_probe.py --out results/bbr-probe --reps 10
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import lfn

CASES = [
    ("bbr", 1500, 4),  # the one that stalls
    ("bbr", 9000, 4),  # same flows, 6x fewer packets
    ("cubic", 1500, 4),  # same packet rate, different congestion control
]


def cpu_busy() -> tuple[int, int]:
    fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    return sum(fields) - idle, sum(fields)


def qdisc_stats(ns_name: str, dev: str) -> dict:
    out = lfn.ns(ns_name, "tc", "-s", "qdisc", "show", "dev", dev)
    stat = {"raw": " ".join(out.split())}
    for key, pattern in (
        ("sent_pkts", r"Sent \d+ bytes (\d+) pkt"),
        ("dropped", r"dropped (\d+)"),
        ("requeues", r"requeues (\d+)"),
        ("backlog_bytes", r"backlog (\d+)b"),
        ("backlog_pkts", r"backlog \S+ (\d+)p"),
    ):
        m = re.search(pattern, out)
        stat[key] = int(m.group(1)) if m else None
    return stat


def flows() -> list[dict]:
    """Per-connection TCP state for the data flows to the receiver (port 5201)."""
    out = lfn.ns(lfn.SND, "ss", "-tin", "dst", lfn.RCV_IP, "dport", "=", "5201")
    lines = out.splitlines()[1:]
    result = []
    for head, info in zip(lines[0::2], lines[1::2]):
        f = {"conn": " ".join(head.split()[3:5])}
        for key, pattern in (
            ("cwnd", r"\bcwnd:(\d+)"),
            ("rtt_ms", r"\brtt:([\d.]+)"),
            ("unacked", r"\bunacked:(\d+)"),
            ("retrans_total", r"\bretrans:\d+/(\d+)"),
            ("delivery_rate", r"\bdelivery_rate (\S+)"),
            ("pacing_rate", r"\bpacing_rate (\S+)"),
            ("bbr_bw", r"bbr:\(bw:(\S+?),"),
            ("bytes_acked", r"\bbytes_acked:(\d+)"),
        ):
            m = re.search(pattern, info)
            f[key] = m.group(1) if m else None
        result.append(f)
    return result


def sample(stop: threading.Event, out: list[dict], t0: float) -> None:
    prev = cpu_busy()
    while not stop.is_set():
        time.sleep(0.5)
        busy, total = cpu_busy()
        cpu = round(100 * (busy - prev[0]) / max(1, total - prev[1]), 1)
        prev = (busy, total)
        out.append(
            {
                "t": round(time.monotonic() - t0, 2),
                "cpu_pct": cpu,
                "sender_qdisc": qdisc_stats(lfn.SND, "veth-a"),
                "receiver_qdisc": qdisc_stats(lfn.RCV, "veth-b"),
                "flows": flows(),
            }
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--seconds", type=int, default=15)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    lfn.setup()
    lfn.buffers("tuned")
    summary = []
    for cc, mtu, streams in CASES:
        applied = lfn.shape(250, 0, mtu)
        rtt = lfn.measured_rtt()
        for rep in range(1, args.reps + 1):
            samples: list[dict] = []
            stop = threading.Event()
            t0 = time.monotonic()
            sampler = threading.Thread(target=sample, args=(stop, samples, t0))
            sampler.start()
            try:
                result = lfn.iperf(cc, streams, args.seconds)
            finally:
                stop.set()
                sampler.join()
            mbps, retx, _ = lfn.summarize(result)
            name = f"{cc}_mtu{mtu}_p{streams}_r{rep}"
            (args.out / f"{name}.json").write_text(
                json.dumps({"iperf": result, "samples": samples, "applied_qdisc": applied, "measured_rtt_ms": rtt})
            )
            peak_cpu = max((s["cpu_pct"] for s in samples), default=None)
            summary.append({"case": name, "mbps": mbps, "retransmits": retx, "peak_cpu_pct": peak_cpu})
            print(f"{name}: {mbps} Mbit/s, {retx} retransmits, peak CPU {peak_cpu}%", flush=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("CPUS", subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip())


if __name__ == "__main__":
    main()
