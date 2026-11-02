"""Compare kernel TCP counters between stalled and healthy runs of the v2 BBR probe.

usage: python partb/probe_counters.py results/bbr-probe/run-<id>
For each test: throughput, how far global TCP memory got against tcp_mem's limits, how much
each TcpExt counter grew during the test (sender and receiver namespaces), and the snd_wnd
the sender saw from the receiver, sampled mid-stall (t = 9-12 s).
"""

import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
summary = {s["case"]: s["mbps"] for s in json.loads((root / "summary.json").read_text())}
for name, mbps in sorted(summary.items(), key=lambda kv: kv[1]):
    d = json.loads((root / f"{name}.json").read_text())
    s = d["samples"]
    mem = [x["tcp_mem_pages"] for x in s if x.get("tcp_mem_pages")]
    peak_mem = max((m["used"] or 0) for m in mem) if mem else None
    limits = (mem[0]["low"], mem[0]["pressure"], mem[0]["high"]) if mem else None

    def growth(side: str) -> dict:
        first, last = s[0][side], s[-1][side]
        return {k: last[k] - first[k] for k in first if first[k] is not None and last.get(k) is not None and last[k] != first[k]}

    mid = [x for x in s if 9 <= x["t"] <= 12]
    snd_wnd = sorted(
        {int(m.group(1)) for x in mid for f in x["flows"] if (m := re.search(r"\bsnd_wnd:(\d+)", f.get("raw", "")))}
    )
    print(f"{name}: {mbps} Mbit/s | peak TCP mem {peak_mem} pages, tcp_mem low/pressure/high {limits}")
    print(f"    receiver grew: {growth('tcpext_receiver')}")
    print(f"    sender grew:   {growth('tcpext_sender')}")
    print(f"    snd_wnd seen 9-12 s: {snd_wnd[:3]}..{snd_wnd[-3:] if snd_wnd else []}")
