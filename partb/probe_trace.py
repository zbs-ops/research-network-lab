"""Print the 0.5 s trace of one bbr_probe test: per-flow TCP state and the netem queues.

usage: python partb/probe_trace.py results/bbr-probe/bbr_mtu1500_p4_r1.json
"""

import json
import sys

d = json.loads(open(sys.argv[1]).read())
print("t     cpu%  snd_backlog_pkts  rcv_backlog_pkts | per flow: cwnd / rtt_ms / unacked / bbr_bw / pacing")
for s in d["samples"]:
    flows = " | ".join(
        f"{f['cwnd']}/{f['rtt_ms']}/{f['unacked']}/{f['bbr_bw']}/{f['pacing_rate']}" for f in s["flows"]
    )
    print(
        f"{s['t']:<5} {s['cpu_pct']:<5} {s['sender_qdisc']['backlog_pkts']!s:<17} "
        f"{s['receiver_qdisc']['backlog_pkts']!s:<16} | {flows}"
    )
