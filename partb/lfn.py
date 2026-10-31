"""Part B: how TCP behaves over a long, lossy, fast path ("long fat network").

Two network namespaces joined by a veth pair stand in for two data-transfer nodes on
either end of a research backbone. tc netem on each side adds half the round-trip
delay; the sender side also adds a 1 Gbit/s bottleneck and random loss. iperf3 then
moves data across, and every result (iperf3 JSON, the measured ping RTT, the exact
netem settings) is saved so each number in the write-up traces to a file.

Run as root on Linux:  python3 partb/lfn.py --rtt 120 --out results/partb
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import re
import subprocess
import time
from pathlib import Path

SND, RCV = "dtn-a", "dtn-b"  # data transfer nodes
SND_IP, RCV_IP = "10.9.0.1", "10.9.0.2"
RATE = "1gbit"
FIELDS = [
    "rtt_ms", "loss_pct", "cc", "mtu", "streams", "buffers", "rep",
    "measured_rtt_ms", "throughput_mbps", "retransmits", "netem_dropped", "duration_s", "raw_file",
]  # fmt: skip


def num(x: float) -> float | int:
    """120.0 -> 120, so CSV keys match however the value was typed."""
    return int(x) if float(x).is_integer() else x


def run(*cmd: str, check: bool = True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout


def ns(name: str, *cmd: str, check: bool = True) -> str:
    return run("ip", "netns", "exec", name, *cmd, check=check)


def setup() -> None:
    run("ip", "netns", "del", SND, check=False)
    run("ip", "netns", "del", RCV, check=False)
    run("ip", "netns", "add", SND)
    run("ip", "netns", "add", RCV)
    run("ip", "link", "add", "veth-a", "netns", SND, "type", "veth", "peer", "name", "veth-b", "netns", RCV)
    ns(SND, "ip", "addr", "add", f"{SND_IP}/24", "dev", "veth-a")
    ns(RCV, "ip", "addr", "add", f"{RCV_IP}/24", "dev", "veth-b")
    for n, dev in ((SND, "veth-a"), (RCV, "veth-b")):
        ns(n, "ip", "link", "set", "lo", "up")
        ns(n, "ip", "link", "set", dev, "up")
        # Segmentation offloads make one "packet" up to 64 KB, which netem would then delay and
        # drop as a unit. Turning them off makes loss and the rate limit act on real-size packets.
        ns(n, "ethtool", "-K", dev, "tso", "off", "gso", "off", "gro", "off", check=False)


def shape(rtt_ms: float, loss_pct: float, mtu: int) -> dict:
    """Apply netem on both sides. Returns exactly what was applied, for the record."""
    half = f"{rtt_ms / 2}ms"
    # The queue must hold at least one bandwidth-delay product, or netem itself drops packets
    # and the experiment measures the emulator, not TCP. 1 Gbit/s x RTT, in packets, x2 headroom.
    bdp_packets = int(1e9 * rtt_ms / 1000 / 8 / mtu)
    limit = max(1000, 2 * bdp_packets)
    for n, dev in ((SND, "veth-a"), (RCV, "veth-b")):
        ns(n, "ip", "link", "set", dev, "mtu", str(mtu))
        ns(n, "tc", "qdisc", "del", "dev", dev, "root", check=False)
    snd_args = ["delay", half, "rate", RATE, "limit", str(limit)]
    if loss_pct:
        snd_args += ["loss", f"{loss_pct}%"]
    ns(SND, "tc", "qdisc", "add", "dev", "veth-a", "root", "netem", *snd_args)
    ns(RCV, "tc", "qdisc", "add", "dev", "veth-b", "root", "netem", "delay", half, "limit", str(limit))
    return {
        "sender_qdisc": ns(SND, "tc", "qdisc", "show", "dev", "veth-a").strip(),
        "receiver_qdisc": ns(RCV, "tc", "qdisc", "show", "dev", "veth-b").strip(),
    }


def measured_rtt() -> float:
    """Average ping RTT across the emulated path, so the delay is checked, not assumed.

    One ping first, not counted: the very first packet waits for ARP, which itself crosses the
    delayed path, and run 41176590070 showed it inflating the average by ~20% (144 ms at 120)."""
    ns(SND, "ping", "-c", "1", "-W", "5", RCV_IP, check=False)
    out = ns(SND, "ping", "-c", "5", "-i", "0.2", "-q", RCV_IP)
    m = re.search(r"= [\d.]+/([\d.]+)/", out)
    assert m, out
    return float(m.group(1))


def buffers(kind: str) -> None:
    """'default' is what Ubuntu ships (max ~6 MB receive buffer). 'tuned' raises the ceilings to
    256 MB so a single connection can hold a full bandwidth-delay product at 250 ms (~31 MB)."""
    big = 256 * 1024 * 1024
    values = {
        "default": {"net.core.rmem_max": "212992", "net.core.wmem_max": "212992",
                    "net.ipv4.tcp_rmem": "4096 131072 6291456", "net.ipv4.tcp_wmem": "4096 16384 4194304"},
        "tuned": {"net.core.rmem_max": str(big), "net.core.wmem_max": str(big),
                  "net.ipv4.tcp_rmem": f"4096 131072 {big}", "net.ipv4.tcp_wmem": f"4096 16384 {big}"},
    }[kind]  # fmt: skip
    for key, val in values.items():
        run("sysctl", "-qw", f"{key}={val}")  # net.core.* is host-wide
        if key.startswith("net.ipv4"):
            for n in (SND, RCV):
                ns(n, "sysctl", "-qw", f"{key}={val}")  # net.ipv4.tcp_* is per namespace


def iperf(cc: str, streams: int, seconds: int) -> dict:
    server = subprocess.Popen(["ip", "netns", "exec", RCV, "iperf3", "-s", "-1"], stdout=subprocess.DEVNULL)
    time.sleep(0.5)
    try:
        out = ns(SND, "iperf3", "-c", RCV_IP, "-t", str(seconds), "-P", str(streams), "-C", cc, "-J")
    finally:
        server.wait(timeout=seconds + 30)
    return json.loads(out)


def netem_drops() -> int:
    """Packets the sender-side netem queue itself dropped (queue full), as opposed to the
    random loss we asked for. If this grows during a test, the emulator was the bottleneck."""
    out = ns(SND, "tc", "-s", "qdisc", "show", "dev", "veth-a")
    m = re.search(r"dropped (\d+)", out)
    return int(m.group(1)) if m else -1


def summarize(result: dict) -> tuple[float, int, float]:
    """Throughput the receiver actually got (Mbit/s), sender retransmits, test length."""
    end = result["end"]
    return (
        round(end["sum_received"]["bits_per_second"] / 1e6, 1),
        int(end["sum_sent"].get("retransmits", 0)),
        round(end["sum_received"]["seconds"], 2),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtt", type=float, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--loss", type=float, nargs="+", default=[0, 0.01, 0.1])
    args = ap.parse_args()

    raw = args.out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    rows = []
    setup()

    # Main grid: tuned buffers, every combination of loss x congestion control x MTU x streams.
    grid = [(loss, cc, mtu, streams, "tuned") for loss, cc, mtu, streams in
            itertools.product(args.loss, ["cubic", "bbr"], [1500, 9000], [1, 4])]  # fmt: skip
    # Buffer comparison: one clean single stream, default vs tuned ceilings.
    grid += [(0, cc, 1500, 1, b) for cc in ("cubic", "bbr") for b in ("default",)]

    for loss, cc, mtu, streams, buf in grid:
        buffers(buf)
        applied = shape(args.rtt, loss, mtu)
        rtt = measured_rtt()
        for rep in range(1, args.reps + 1):
            name = f"rtt{num(args.rtt)}_loss{num(loss)}_{cc}_mtu{mtu}_p{streams}_{buf}_r{rep}.json"
            drops_before = netem_drops()
            result = iperf(cc, streams, args.seconds)
            netem_dropped = netem_drops() - drops_before
            result["lab"] = {"applied_qdisc": applied, "measured_rtt_ms": rtt, "netem_dropped": netem_dropped}
            (raw / name).write_text(json.dumps(result))
            mbps, retx, dur = summarize(result)
            row = dict(zip(FIELDS, [num(args.rtt), num(loss), cc, mtu, streams, buf, rep, rtt, mbps, retx, netem_dropped, dur, name]))
            rows.append(row)
            print(f"{name}: {mbps} Mbit/s, {retx} retransmits, netem dropped {netem_dropped}, ping RTT {rtt} ms", flush=True)

    with open(args.out / f"results_rtt{int(args.rtt)}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
