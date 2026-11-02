# research-network-lab

A research-network engineer's lab in one repo, on free tools, with every number traced to a CI run you can open.

- **Part A, fabric as code:** a leaf-spine fabric (Containerlab + FRRouting, eBGP unnumbered, BFD, ECMP), tests that check routing on every push, and four planned failures with a written diagnosis for each. [docs/part-a-failures.md](docs/part-a-failures.md)
- **Part B, long-distance transfers:** how one TCP transfer behaves over an emulated 1 Gbit/s path at 10, 120 and 250 ms with packet loss. CUBIC vs BBR, buffer tuning, MTU 1500 vs 9000, single vs parallel streams, and a traced-to-the-kernel explanation of why 4 BBR streams sometimes stall. [docs/part-b-results.md](docs/part-b-results.md)
- **Part C, monitoring:** a Prometheus exporter, four alert rules and a Grafana dashboard, tested by breaking the fabric on purpose. [docs/part-c-monitoring.md](docs/part-c-monitoring.md)

The write-up: [docs/article.md](docs/article.md). Japanese summary: [docs/summary-ja.md](docs/summary-ja.md).

## Headline results

| | Result | Run |
|---|---|---|
| Spine loses both links | detected in 0.08 s, 0 pings lost of 150 | [41171994859](https://github.com/zbs-ops/research-network-lab/actions/runs/41171994859) |
| Link up but dropping everything | BFD caught it in 0.78 s (BGP hold timer: 9 s) | same |
| 250 ms, 0.1% loss, one stream | CUBIC 33 Mbit/s vs BBR 707 (median of 6) | [41176590070](https://github.com/zbs-ops/research-network-lab/actions/runs/41176590070), [41178340217](https://github.com/zbs-ops/research-network-lab/actions/runs/41178340217) |
| 250 ms, no loss, one stream | default buffers 81 Mbit/s vs 256 MB ceiling 791 | same |
| 4 BBR streams stalling at 250 ms | caused by receiver TCP memory pressure; 4× `tcp_mem` took stalls from 3/12 to 0/12 | [41390596751](https://github.com/zbs-ops/research-network-lab/actions/runs/41390596751) |
| BGP up, routes gone (policy mistake) | PrefixMissing alert in 3.5–5.5 s; BGP alerts correctly silent | [41388887136](https://github.com/zbs-ops/research-network-lab/actions/runs/41388887136) |

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

Every leaf links to both spines. BGP runs on unnumbered links (IPv6 link-local sessions carrying IPv4 routes), so there are no point-to-point subnets to manage, and BFD detects a dead link in under a second. Each leaf announces its rack subnet and keeps one path through each spine (ECMP).

## Run it

Everything runs in GitHub Actions on every push. Each workflow starts by checking that the runner supports what the lab needs (`scripts/check_runner.sh`): Docker, a real netem qdisc, network namespaces, BBR, MTU 9000.

| Workflow | What it does |
|---|---|
| `fabric.yml` | builds the fabric, checks BGP, ECMP and reachability, runs the four failures |
| `partb.yml` | the transfer grid at 10, 120 and 250 ms (one job each), then the plots |
| `bbr-probe.yml` | the instrumented BBR stall probe, normal vs 4× `tcp_mem` side by side |
| `monitoring.yml` | fabric plus Prometheus and Grafana, alert tests, dashboard screenshot |

Locally (Linux, root, Docker, Containerlab, iperf3):

```
sudo containerlab deploy -t topology/fabric.clab.yml
sudo python -m pytest -v tests/test_fabric.py tests/test_failures.py
cd partb && sudo python3 lfn.py --rtt 250 --out ../results/partb-local
```

## How this was built

I set the goal (a lab that shows the day-to-day work of a research-network engineer), the scope, and the rules: every number comes from a CI run that's linked next to it, check the environment before trusting it, and write down what broke. The building was done by Friday, an AI agent I run. Friday wrote the topology, the router configs, the tests, the measurement scripts and the write-ups, ran every experiment, and posted each phase's real output for me to review before the next phase started.

**Where it runs.** The agent's machine has no Docker and no `tc`, and neither does my laptop. So every push builds the lab on a fresh GitHub Actions runner, which has root, Docker and the kernel's netem module. That's why "run it in CI" is the reproduction path, and why a fresh run can always check the results.

**Problems hit, and the fixes:**

- **Results came back empty.** GitHub's job-log API returns plain text. The first log reader tried to parse it as JSON and silently got nothing. Fixed by reading the raw text.
- **A route looked like it had one path, not two.** The CI log printed each prefix's first next-hop line only, because a filter dropped the continuation line. The ECMP claim comes from the test, which counts active next hops (2) on the live router, not from that log.
- **A "silent" failure needs the link to stay up.** There's no guarantee `tc` exists inside the FRR image, so the test applies `netem loss 100%` from the runner into the router's network namespace with `nsenter`. It also asserts the interface still reads `up`, so the scenario can't quietly turn into an ordinary link-down.
- **A config error hiding behind a working network.** BGP and ECMP worked, but every router logged `frr.conf processing failure: 11`. FRR's return code 11 is `CMD_ERR_NO_FILE`, and the line before it said `/etc/frr/vtysh.conf` was missing. That guess was tested: adding a one-line `vtysh.conf` made the message disappear ([run 41173627135](https://github.com/zbs-ops/research-network-lab/actions/runs/41173627135)), and CI now fails if it comes back.
- **A check that passed without proving anything.** The first MTU 9000 check printed a line that didn't show the MTU. It now prints `mtu 9000` from the interface itself.
- **The measured RTT was 20% high** on the first test of each Part B run (144 ms at a 120 ms target). The first ping waited for ARP across the delayed path. One uncounted warm-up ping fixed it; every later RTT is within 0.1 ms of target.
- **Sender-side numbers lie with big buffers.** iperf3's per-second sender figures showed 2,234 Mbit/s on a 1 Gbit/s link: that's data written into a 256 MB socket buffer, not data delivered. Every reported number is the receiver's.
- **An unexplained result became the most interesting finding.** 4 BBR streams at 250 ms sometimes stalled with zero loss. Instead of calling it noise, a probe traced it to the receiver's TCP memory pressure, then an intervention (4× `tcp_mem`) confirmed it. See Part B, section 5.
- **The dashboard misreported events.** Up links showed red, one panel didn't render, and a 6.3 Mbit/s burst showed as about 400 kb/s, because Grafana assumed 15 s between points instead of the real 2 s. Each was caught in a CI screenshot and fixed, and the next run checked the fix.
- **A number written before it was checked.** A draft said the runner had "7 GB" of RAM. `free -m` says 7,932 MB, and the docs now say so.

Two things stay unexplained and aren't used for any claim: one BBR test with 106,267 retransmits that never recurred, and netem's own drop counter, which reports drops in tests that had zero retransmits.
