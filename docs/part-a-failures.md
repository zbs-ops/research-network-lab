# Part A: breaking the fabric on purpose

Four planned failures, each run by `tests/test_failures.py` on the live lab in CI. In the first three, host1 pings host2 every 100 ms (150 pings) while the failure happens, so "pings lost" is what one application flow would have felt. The fourth scenario checks reachability with a short ping instead.

The fabric workflow runs these tests on every push. The table covers all 22 runs from [run 41171994859](https://github.com/zbs-ops/research-network-lab/actions/runs/41171994859) to [run 41391387245](https://github.com/zbs-ops/research-network-lab/actions/runs/41391387245), with the same test code throughout. Each run's numbers are in `results/failures-all-runs.json`; the first run's are also in `results/failures.json`. Times are the median, with the range in brackets.

| Scenario | Route change seen | Back to two paths after the fix | Pings lost (of 150) |
|---|---|---|---|
| Spine down (both links dark) | 0.085 s [0.06–0.12] | 3.5 s [2.9–4.5] | 0 in 18 runs, 1 in 4 |
| Silent link failure (100% loss, link still up) | 0.90 s [0.74–1.06] | 2.4 s [1.9–2.5] | 0 in 13 runs, 8–10 in 9 |
| Link flap x3 (leaf1 to spine1) | n/a | 2.9 s [1.8–4.2] | 0–2 |
| BGP misconfig (leaf2 stops announcing) | 0.67 s [0.62–0.68] | 0.67 s [0.61–0.68] | all traffic to rack 2 blackholed until fixed |

**How the times are measured.** The test polls leaf1's routing table (`show ip route 10.1.2.0/24 json` through `docker exec`) until the number of active next hops changes. It polls every 0.5 s, and every 0.1 s in the silent-failure test, and each poll itself takes several tens of milliseconds. So every time is an upper bound at that resolution. The spine-down figure means the route had already changed at the first check. The misconfig's 0.62–0.68 s is the second check, which is also why its detection and recovery times look identical.

## 1. Spine down

**What broke:** both of spine1's links went down, as if it lost power.

**How it showed up:** leaf1's route to 10.1.2.0/24 had dropped from two next hops to one by the first check (at most 0.12 s in any run). The BGP sessions to spine1 go down with the links.

**Diagnosis:** `show bgp summary` on either leaf shows the sessions to one spine down and the other spine fine. `show ip route 10.1.2.0/24` shows a single path. One spine missing from every leaf's view means the spine, not a leaf link.

**Why almost nothing was lost:** with two spines and ECMP, the surviving path was already installed. When the links go down, the kernel and BGP drop the dead path at once. Losing a spine halves capacity; it doesn't cut anything off. At most 1 ping in 150 was lost in any run.

## 2. Silent link failure

**What broke:** spine1's link to leaf1 kept carrier but dropped every packet (`tc netem loss 100%` on spine1's side), like a dying optic or a broken middlebox.

**How it showed up:** the interface still read `up` (the test asserts this). BFD declared the peer down, BGP dropped the session, and leaf1 went to one path 0.74–1.06 s after the loss started. That matches FRR's default BFD timers, which this lab doesn't change: 300 ms intervals, 3 missed packets.

**Pings lost:** either 0 or 8–10, never in between. The ping is a single flow, and ECMP hashes it onto one path. When the flow's path crossed the broken direction, it lost packets until BFD reacted (8–10 pings at 10 per second is 0.8–1.0 s). When it didn't, it lost nothing. The test doesn't record which path the pings took, so this explanation fits the numbers but wasn't measured directly.

**Diagnosis:** the interface looks healthy, so interface monitoring alone misses this. `show bfd peers` shows the session down, and BGP logs show the neighbor going down with a BFD reason.

**Why it matters:** without BFD, the BGP hold timer is the only detector. This lab sets it to 9 s (`timers 3 9`). Common defaults are much longer: 180 s in FRR's traditional profile and Cisco IOS, 90 s in Junos. BFD turns that into about a second. The test fails if detection takes 3 s or more.

## 3. Link flap

**What broke:** leaf1's link to spine1 went down and up three times, one second apart.

**How it showed up:** both paths were back 1.8–4.2 s after the last flap, and the ping lost 0–2 packets.

**Diagnosis:** repeated session resets on one interface while its partner stays stable point at that one link (cable, optic, port). In production you'd check the interface error counters and flap count before blaming BGP.

## 4. BGP misconfig

**What broke:** an outbound route-map on leaf2 denying everything. This is the classic typo in a policy change.

**How it showed up:** both of leaf2's sessions stayed Established (the test asserts 2 of 2), so every "is BGP up?" check stays green. leaf1 lost its route to 10.1.2.0/24 by the second check (0.62–0.68 s), and host1 couldn't reach host2.

**Diagnosis:** sessions up but the prefix missing means policy, not transport. `show bgp ipv4 unicast neighbors eth1 advertised-routes` on leaf2 is empty. That's the smoking gun: leaf2 has the route but isn't sending it. `show route-map` shows the deny.

**Fix:** remove the route-map and soft-reset outbound. leaf1 had both paths back by the second check, with no session flap.

**Lesson:** monitor that expected prefixes are present, not just that sessions are up. Part C adds exactly that alert.

## Limits

- Times are upper bounds set by the polling interval (see above), not precise convergence times.
- The test measures route counts, ping loss, interface state, and (scenario 4) session count and advertised routes. The BFD and BGP session events described for scenarios 1 to 3 are standard FRR behaviour; the test doesn't record them.
- One ping flow is one ECMP path. "0 pings lost" says that flow wasn't hurt, not that the failure was hitless for all traffic.
- This is a 2x2 lab on one machine. Real optics, real ASIC programming time, and larger tables make every number here a lower bound.
