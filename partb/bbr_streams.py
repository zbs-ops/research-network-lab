"""Look inside the 4-stream BBR tests at 250 ms: which stream collapses, and when?

usage: python partb/bbr_streams.py results/partb
Prints, for every 4-stream BBR test at 250 ms, each stream's received throughput, and the
per-second total for the first seconds of the run. Reads the raw iperf3 JSON only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    files = sorted(root.glob("run-*/raw/rtt250_*_bbr_mtu*_p4_tuned_r*.json"))
    for f in files:
        d = json.loads(f.read_text())
        total = d["end"]["sum_received"]["bits_per_second"] / 1e6
        per_stream = [s["receiver"]["bits_per_second"] / 1e6 for s in d["end"]["streams"]]
        retx = [s["sender"].get("retransmits", 0) for s in d["end"]["streams"]]
        # Interval sums are the sender's view (bytes handed to the socket), so only the shape
        # over time is used from them, not their level.
        timeline = [round(iv["sum"]["bits_per_second"] / 1e6) for iv in d["intervals"]]
        first_nonzero = next((i for i, v in enumerate(timeline) if v > 0), None)
        stalls = sum(1 for v in timeline if v == 0)
        print(
            f"{f.parent.parent.name} {f.name}: total {total:.0f} | streams "
            + " ".join(f"{s:.0f}" for s in per_stream)
            + f" | retx {retx} | zero-seconds {stalls} | first data at {first_nonzero}s"
        )
        print("    sender per-second:", timeline)


if __name__ == "__main__":
    main()
