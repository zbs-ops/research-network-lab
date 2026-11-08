# research-network-lab

A research-network engineer's lab in one repo, on free tools. Every number is traced to a GitHub Actions run, and the raw data from each run is committed under `results/`.

- **Part A, fabric as code:** a leaf-spine fabric (Containerlab + FRRouting, eBGP unnumbered, BFD, ECMP), tests that check routing on every push, and four planned failures with a written diagnosis for each. [docs/part-a-failures.md](docs/part-a-failures.md)
- **Part B, long-distance transfers:** how one TCP transfer behaves over an emulated 1 Gbit/s path at 10, 120 and 250 ms with packet loss. CUBIC vs BBR, buffer tuning, MTU 1500 vs 9000, single vs parallel streams, and why 4 BBR streams sometimes stall, traced through kernel TCP counters. [docs/part-b-results.md](docs/part-b-results.md)
- **Part C, monitoring:** a Prometheus exporter, four alert rules and a Grafana dashboard, tested by breaking the fabric on purpose. [docs/part-c-monitoring.md](docs/part-c-monitoring.md)
- **Part D, Raft under network faults:** etcd clusters of 3 and 5 members, one network namespace each, with the leader killed or cut off under emulated delay and loss, and minority/majority partitions. How long there's no leader, what a steady writer sees, which side keeps working, and a read-back of every acknowledged write. [docs/part-d-raft.md](docs/part-d-raft.md)

The write-up: [docs/article.md](docs/article.md). Japanese summary: [docs/summary-ja.md](docs/summary-ja.md).

## Headline results

| | Result | Run |
|---|---|---|
| Spine loses both links | leaf down to one path by the first check (≤ 0.12 s); at most 1 of 150 pings lost | 22 fabric runs: [results/failures-all-runs.json](results/failures-all-runs.json) |
| Link up but dropping everything | BFD-driven failover in 0.74–1.06 s (BGP hold timer: 9 s) | same |
| 250 ms, 0.1% loss, one stream | CUBIC 33 Mbit/s vs BBR 707 (median of 6) | [41176590070](https://github.com/zbs-ops/research-network-lab/actions/runs/41176590070), [41178340217](https://github.com/zbs-ops/research-network-lab/actions/runs/41178340217) |
| 250 ms, no loss, one stream | default buffers 81 Mbit/s vs 256 MB ceiling 791 | same |
| 4 BBR streams stalling at 250 ms | caused by receiver TCP memory pressure; 4× `tcp_mem` took stalls from 3/12 to 0/12 (all probe runs: 10/34 vs 0/12, one-sided Fisher p = 0.03) | [41390596751](https://github.com/zbs-ops/research-network-lab/actions/runs/41390596751) |
| BGP up, routes gone (policy mistake) | PrefixMissing alert in 3.5–5.5 s; BGP alerts silent, in all 11 runs | 11 monitoring runs: [results/monitoring/all-runs.json](results/monitoring/all-runs.json) |
| etcd leader killed, 3 members, no added delay | new leader in 1.05 s with etcd's default timeouts, 0.66 s with 50/500 ms (median of 10) | [42452414571](https://github.com/zbs-ops/research-network-lab/actions/runs/42452414571), [42454644849](https://github.com/zbs-ops/research-network-lab/actions/runs/42454644849) |
| Same, 100 ms one-way between members | the faster timeouts lose: 2.31 s vs 1.85 s, median 4 elections vs 1 (split votes between the two survivors) | same |
| Partitions, 3 and 5 members | the minority acknowledged 0 writes in all 120 trials; 1,231,115 acknowledged writes across Part D, 0 lost | same |
| Follower cut off for 10 s, then healed (3 members, 0 ms) | 3.07 s before it serves writes again, set by TCP retransmission backoff on the old connection, not by Raft | same |

## What this shows for the role

- **Routing:** an eBGP leaf-spine fabric in FRRouting (unnumbered sessions, BFD, ECMP), with configs in `configs/` and tests on every push that check sessions, next-hop counts and host reachability.
- **Failure handling:** four injected failures, including a link that stays up while dropping everything and a route-policy mistake, each with the commands that diagnose it and timings from 22 CI runs.
- **Monitoring:** a Prometheus exporter, four alert rules and a Grafana dashboard, tested by breaking the fabric. A prefix-presence alert catches the failure that session monitoring misses.
- **Distributed control planes:** Raft (etcd) failover timed from etcd's own log against delay and loss, write availability through the fault, partition behaviour, and a seconds-long recovery after a partition heals traced to TCP backoff with `ss`.
- **High-throughput data transfer:** TCP over emulated 10, 120 and 250 ms paths at 1 Gbit/s: congestion control, socket buffers, MTU and parallel streams, plus a BBR stall traced to kernel `tcp_mem` pressure and confirmed by an intervention.

What it doesn't show: everything is emulated, each experiment on a single cloud VM. There are no hardware routers, real long-distance paths or production traffic here.

## The fabric

```
        spine1 (AS 65000)     spine2 (AS 65000)
          |        \          /        |
          |         \        /         |
          |          \      /          |
        leaf1 (AS 65101)    leaf2 (AS 65102)
          |                            |
        host1 10.1.1.10            host2 10.1.2.10
```

Every leaf links to both spines. BGP runs on unnumbered links (IPv6 link-local sessions carrying IPv4 routes), so there are no point-to-point subnets to manage, and BFD detects a dead link in about a second. Each leaf announces its rack subnet and keeps one path through each spine (ECMP).

## Run it

Everything runs in GitHub Actions (`.github/workflows/`). `fabric.yml`, `partb.yml` and `bbr-probe.yml` start by checking that the runner supports what the lab needs (`scripts/check_runner.sh`): Docker, a real netem qdisc, network namespaces, BBR, MTU 9000. `partd.yml` runs `scripts/check_partd.sh` (namespaces, netem and iptables inside a namespace, the etcd binary) and commits its output with the data.

| Workflow | When it runs | What it does |
|---|---|---|
| `fabric.yml` | every push | builds the fabric, checks BGP, ECMP and reachability, runs the four failures |
| `monitoring.yml` | every push | fabric plus Prometheus and Grafana, alert tests, dashboard screenshot |
| `partb.yml` | changes under `partb/`, or manually | the transfer grid at 10, 120 and 250 ms (one job each), then the plots |
| `bbr-probe.yml` | changes to the probe, or manually | the instrumented BBR stall probe, normal vs 4× `tcp_mem` side by side |
| `partb-plots.yml` | changes to the Part B data or plot script, or manually | rebuilds Part B's summaries and figures from the committed CSVs |
| `partd.yml` | manually | the Raft grid: 3 and 5 members, two timeout pairs, killed or isolated leader under delay and loss, plus partitions (ten runners, 30–40 minutes) |
| `partd-plots.yml` | changes to the Part D data or analysis, or manually | rebuilds Part D's summaries and figures from the committed trial files |

To rerun one, open the Actions tab, pick the workflow and use "Run workflow". GitHub deletes run logs after its retention period (90 days by default), so the run links will eventually stop showing logs; the data stays in `results/`.

Locally, the same steps as the workflows (Linux with root, Docker, Containerlab 0.59.0, iperf3, ethtool, Python 3 with pytest). These have only been run on GitHub's `ubuntu-24.04` runners:

```
sh scripts/check_runner.sh
sudo containerlab deploy -t topology/fabric.clab.yml
sudo -E env "PATH=$PATH" python3 -m pytest -v tests/test_fabric.py tests/test_failures.py
sudo containerlab destroy -t topology/fabric.clab.yml

sudo modprobe tcp_bbr
sudo python3 partb/lfn.py --rtt 250 --out /tmp/partb-local
python3 partb/plot.py /tmp/partb-local /tmp/partb-local/plots   # needs matplotlib
```

The failure test rewrites `results/failures.json`. For Part C, follow the steps in `monitoring.yml`.

## How this was built

I set the goal (a lab that shows the day-to-day work of a research-network engineer), the scope, and the rules: every number comes from a CI run that's linked next to it, check the environment before trusting it, and write down what broke. The building was done by Friday, an AI agent I run. Friday wrote the topology, the router configs, the tests, the measurement scripts and the write-ups, ran every experiment, and posted each phase's real output for me to review before the next phase started.

**Where it runs.** The agent's machine has no Docker and no `tc`, and neither does my laptop. So every push builds the lab on a fresh GitHub Actions runner, which has root, Docker and the kernel's netem module. That's why "run it in CI" is the reproduction path, and why anyone can rerun a workflow to check the results.

**Problems hit, and the fixes:**

- **Results came back empty.** GitHub's job-log API returns plain text. The first log reader tried to parse it as JSON and silently got nothing. Fixed by reading the raw text.
- **A route looked like it had one path, not two.** The CI log printed each prefix's first next-hop line only, because a filter dropped the continuation line. The ECMP claim comes from the test, which counts active next hops (2) on the live router, not from that log.
- **A "silent" failure needs the link to stay up.** There's no guarantee `tc` exists inside the FRR image, so the test applies `netem loss 100%` from the runner into the router's network namespace with `nsenter`. It also asserts the interface still reads `up`, so the scenario can't quietly turn into an ordinary link-down.
- **A config error hiding behind a working network.** BGP and ECMP worked, but every router logged `frr.conf processing failure: 11`. FRR's return code 11 is `CMD_ERR_NO_FILE`, and the line before it said `/etc/frr/vtysh.conf` was missing. That guess was tested: adding a one-line `vtysh.conf` made the message disappear ([run 41173627135](https://github.com/zbs-ops/research-network-lab/actions/runs/41173627135)), and CI now fails if it comes back.
- **A check that passed without proving anything.** The first MTU 9000 check printed a line that didn't show the MTU. It now prints `mtu 9000` from the interface itself.
- **The measured RTT was 20–24% high** for the first configuration at each RTT in the first Part B run (144 ms at a 120 ms target). The first ping waited for ARP across the delayed path. One uncounted warm-up ping fixed it; in the second run every RTT is within 0.13 ms of target.
- **One run's plots counted another run's data.** The Part B jobs uploaded `results/partb/`, which already held the first run's committed CSVs, so the second run's summary counted the first run three times. A review pass found it. The jobs now write outside the checkout, and `partb-plots.yml` rebuilds every committed Part B figure from the committed CSVs.
- **Sender-side numbers lie with big buffers.** iperf3's per-second sender figures showed 2,234 Mbit/s on a 1 Gbit/s link: that's data written into a 256 MB socket buffer, not data delivered. Every reported number is the receiver's.
- **An unexplained result became the most interesting finding.** 4 BBR streams at 250 ms sometimes stalled with zero loss. Instead of calling it noise, a probe traced it to the receiver's TCP memory pressure, then an intervention (4× `tcp_mem`) confirmed it. See Part B, section 5.
- **The dashboard misreported events.** Up links showed red, one panel didn't render, and a 6.3 Mbit/s burst showed as about 400 kb/s, because Grafana assumed 15 s between points instead of the real 2 s. Each was caught in a CI screenshot and fixed, and the next run checked the fix.
- **A number written before it was checked.** A draft said the runner had "7 GB" of RAM. `free -m` says 7,932 MB, and the docs now say so.

Two things stay unexplained and aren't used for any claim: three BBR tests in the first grid run with tens to hundreds of thousands of retransmits on single streams, which didn't recur in the second run, and netem's own drop counter, which reports drops in tests that had zero retransmits.
