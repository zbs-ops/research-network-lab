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

## How I build

I work with an AI coding assistant as a pair programmer. I decide what gets built, review every change, and own what ships.
