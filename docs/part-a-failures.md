# Part A: breaking the fabric on purpose

Four planned failures, each run by `tests/test_failures.py` on the live lab in CI. host1 pings host2 every 100 ms throughout, so "pings lost" is what an application would have felt. Numbers are from one CI run ([run 41171994859](https://github.com/zbs-ops/research-network-lab/actions/runs/41171994859)); raw data in `results/failures.json`.

| Scenario | Detected | Recovered after fix | Pings lost (of 150) |
|---|---|---|---|
| Spine down (both links dark) | 0.08 s | 2.96 s | 0 |
| Silent link failure (100% loss, link still up) | 0.78 s | 2.39 s | 0 |
| Link flap x3 (leaf1 to spine1) | n/a | 2.95 s | 0 |
| BGP misconfig (leaf2 stops announcing) | 0.65 s | 0.65 s | all traffic to rack 2 blackholed until fixed |

## 1. Spine down

**What broke:** both of spine1's links went down, as if it lost power.

**How it showed up:** leaf1's route to 10.1.2.0/24 dropped from two next hops to one within 0.08 s. The BGP sessions to spine1 went Idle.

**Diagnosis:** `show bgp summary` on either leaf shows the sessions to one spine down and the other spine fine. `show ip route 10.1.2.0/24` shows a single path. One spine missing from every leaf's view means the spine, not a leaf link.

**Why nothing was lost:** with two spines and ECMP, the surviving path was already installed. Losing a spine halves capacity; it doesn't cut anything off.

## 2. Silent link failure

**What broke:** spine1's link to leaf1 kept carrier but dropped every packet (`tc netem loss 100%`), like a dying optic or a broken middlebox.

**How it showed up:** the interface still read `up`. BFD declared the peer down at 0.78 s, BGP dropped the session, and leaf1 went to one path.

**Diagnosis:** the interface looks healthy, so interface monitoring alone misses this. `show bfd peers` shows the session down, and BGP logs show the neighbor going down with a BFD reason.

**Why it matters:** without BFD, the 9 s BGP hold timer would have been the only detector. With the default 3 s timer most stacks ship, it's 90 s. BFD turns a minute-plus blackhole into under a second. The test fails if detection takes 3 s or more.

## 3. Link flap

**What broke:** leaf1's link to spine1 went down and up three times, one second apart.

**How it showed up:** the session on leaf1 eth1 reset three times. The path through spine2 never moved, and both paths were back 2.95 s after the last flap.

**Diagnosis:** repeated session resets on one interface while its partner stays stable point at that one link (cable, optic, port). In production you'd check the interface error counters and flap count before blaming BGP.

## 4. BGP misconfig

**What broke:** an outbound route-map on leaf2 denying everything. This is the classic typo in a policy change.

**How it showed up:** both of leaf2's sessions stayed Established, so every "is BGP up?" check stays green. leaf1 lost its route to 10.1.2.0/24 within 0.65 s, and host1 couldn't reach host2.

**Diagnosis:** sessions up but the prefix missing means policy, not transport. `show bgp neighbors eth1 advertised-routes` on leaf2 is empty. That's the smoking gun: leaf2 has the route but isn't sending it. `show route-map` shows the deny.

**Fix:** remove the route-map and soft-reset outbound. Routes were back in 0.65 s with no session flap.

**Lesson:** monitor that expected prefixes are present, not just that sessions are up. Part C adds exactly that alert.

## Limits

- One run per scenario so far. The times will vary by tens of milliseconds between runs.
- This is a 2x2 lab on one machine. Real optics, real ASIC programming time, and larger tables make every number here a lower bound.
