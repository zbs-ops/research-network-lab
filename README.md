# research-network-lab

A research-network engineer's lab in one repo, on free tools:

- **Part A, fabric as code:** a leaf-spine data-center fabric (Containerlab + FRRouting, eBGP) with tests that check routing after every change, and planned failures with a written diagnosis for each.
- **Part B, long-distance transfers:** how TCP behaves over emulated long, lossy paths (tc netem): CUBIC vs BBR, buffer tuning, MTU 1500 vs 9000, single vs parallel streams.
- **Part C, monitoring:** Prometheus and Grafana on the fabric, with link-down and throughput alerts.

Status: Part A in progress.

## Part A: the fabric

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

Run it (Linux, Docker, Containerlab):

```
sudo containerlab deploy -t topology/fabric.clab.yml
sudo python -m pytest -v tests/
```

CI builds the same lab on a fresh GitHub runner and runs the tests on every push.

## How this was built

I set the goal (a lab that shows the day-to-day work of a research-network engineer) and the scope (Parts A to C). The building was done by Friday, an AI agent I run. Friday wrote the topology, the router configs, the tests and the write-ups, ran every experiment, and reported back with the numbers. Every number in this repo comes from a CI run that's linked next to it.

**Where it runs.** The agent's machine has no Docker and no `tc`, so nothing could run there. Instead, every push builds the whole lab on a fresh GitHub Actions runner, which has root, Docker and the kernel's netem module. That's why "run it in CI" is the reproduction path, and why a fresh run can always check the results.

**Problems hit, and the fixes:**

- **Results came back empty.** GitHub's job-log API returns plain text. The first log reader tried to parse it as JSON and silently got nothing. Fixed by reading the raw text.
- **A route looked like it had one path, not two.** The CI log printed each prefix's first next-hop line only, because a filter dropped the continuation line. The ECMP claim comes from the test, which counts active next hops (2) on the live router, not from that log.
- **A "silent" failure needs the link to stay up.** Taking an interface down is the easy failure; a link that stays up while dropping everything is the one that hurts in production. There's no guarantee `tc` exists inside the FRR image, so the test applies `netem loss 100%` from the runner into the router's network namespace with `nsenter`. It also asserts that the interface still reads `up`, so the scenario can't quietly turn into an ordinary link-down.
- **Scenarios could contaminate each other.** Each failure test waits for every BGP session and both ECMP paths to come back before the next one starts, so each scenario is measured against a healthy fabric.
