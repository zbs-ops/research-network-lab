# Tuning TCP for long fat networks on an emulated research backbone

*Zohaib Qamar, November 2026. Code, raw data and every CI run: github.com/zbs-ops/research-network-lab*

A research institute moves datasets that are measured in terabytes between campuses, and across oceans to partner institutions. The links are fast. The question I wanted to answer is how much of a fast link one TCP transfer actually gets once the round-trip time reaches a quarter of a second and a little packet loss creeps in, and what an operator can do about it.

I couldn't run real transatlantic links, so the lab emulates them and measures what happens. One disclosure up front, because it matters for how you read the rest: the lab was built and run by Friday, an AI agent I direct. Friday wrote the configs, the test scripts and the first drafts of the write-ups, this article included. I set the questions and the rules (every number must come from a CI run anyone can open, show the environment check before trusting the environment, write down what broke) and approved each phase before the next began. The findings below are what the runs showed. Where we got something wrong, it's in here too.

## The setup

Everything runs on free GitHub Actions runners: 2 vCPUs, 7,932 MB of RAM, Ubuntu with kernel 7.0. My own laptop can't run Docker or `tc`, so the lab had to live somewhere that could. The first CI step proves the runner can do what the lab needs before anything relies on it. It creates a real `netem` qdisc on a dummy interface, reads BBR from the kernel's list of congestion-control algorithms, and reads `mtu 9000` back from a veth interface.

There are three parts:

- **A leaf-spine fabric** (Containerlab, FRRouting 10.1.1): two spines, two leaves, a host behind each leaf, eBGP on unnumbered links with BFD and two equal-cost paths between racks.
- **Long-distance transfers:** two network namespaces joined by a veth pair, with `tc netem` adding delay and loss and a 1 Gbit/s bottleneck, measured with `iperf3`.
- **Monitoring:** a small Prometheus exporter, four alert rules and a Grafana dashboard, tested by breaking the fabric on purpose.

## The fabric, briefly

The fabric part is about failure, not throughput. Four planned breakages. For the first three, one host pinged the other every 100 ms throughout:

- **A spine losing both links:** detected in 0.08 s, with zero pings lost. The leaf already had the second path installed, so traffic just moved.
- **A link that stays up but drops every packet** (`netem loss 100%` on a live interface): the case interface monitoring can't see. BFD declared the peer down in 0.78 s. Without BFD, the 9-second BGP hold timer would have been the only detector.
- **A link flapping three times:** the path through the other spine never moved.
- **A bad outbound route-map on one leaf:** every BGP session stayed Established while a whole rack became unreachable. This one comes back in the monitoring section, because it's the failure a dashboard of green session lights hides.

## What distance and loss do to a single transfer

Each configuration ran 6 times across two separate CI runs. The figures are the median, with the range in brackets, in Mbit/s, on a 1 Gbit/s path.

**Loss is what separates CUBIC from BBR.** With no loss, Linux's default (CUBIC) and Google's BBR are within a few percent of each other at every distance. With 0.1% random loss, one packet in a thousand:

| Round trip | CUBIC | BBR |
|---|---|---|
| 10 ms | 333 [183–419] | 939 [938–939] |
| 120 ms | 111 [22–253] | 853 [846–853] |
| 250 ms | 33 [8–254] | 707 [679–717] |

CUBIC reads every loss as congestion and cuts its window. At 250 ms each recovery takes many seconds, so a single early loss decides most of a 15-second test. That's why its range is so wide. BBR estimates the bottleneck bandwidth and the path's round-trip time directly, so random loss that isn't caused by congestion barely moves it. On a lossy long path the difference is more than 20 times.

**Default socket buffers cap a long transfer.** To fill a 1 Gbit/s path, a sender needs a full bandwidth-delay product in flight: about 15 MB at 120 ms and 31 MB at 250 ms. Ubuntu's default receive-buffer ceiling is 6 MB. With no loss, one CUBIC stream got 179 Mbit/s at 120 ms and 81 at 250 ms on default buffers. With the ceilings raised to 256 MB, it got 884 and 791. That's a fivefold and a tenfold gain, without touching the network.

**Jumbo frames buy what the arithmetic says.** At 10 ms with no loss, MTU 9000 lifted CUBIC from 952 to 989 Mbit/s. netem's rate limit counts the Ethernet header, so a 1500-byte packet carries 1,448 bytes of data in 1,514, and a 9000-byte packet carries 8,948 in 9,014. That predicts 956 and 993. Both measurements are within 4 Mbit/s. Under loss, bigger packets matter more: CUBIC with 4 streams at 120 ms and 0.1% loss went from 284 to 490.

**Parallel streams help CUBIC under loss.** A loss only cuts the window of the stream it hits. At 120 ms and 0.1% loss, 4 streams got 284 against 111 for one. Data-transfer tools open parallel streams for exactly this reason.

## The result that didn't fit

BBR with 4 parallel streams at 250 ms was erratic, and not because of loss. With no loss at all, one run would deliver 806 Mbit/s and the next 201. Some of those runs had zero retransmits. I didn't write a guess into the results. The plan was to trace it until the evidence explained it, and here is the trace.

First, the per-second data already in the iperf3 output. All four streams collapsed together, and from about second 5 the sender handed nothing to the socket for the rest of the test. Ten of the eleven collapses were at MTU 1500.

Second, a probe that sampled every flow's TCP state (`ss`), both netem queues and the host CPU every half second. BBR, 4 streams, MTU 1500 stalled in 4 of 10 tests. MTU 9000 and CUBIC never stalled (10 of 10 each). CPU peaked at 38%. During a stall, each flow had a congestion window of more than 11,000 packets and a pacing rate around 300 Mbit/s, yet only 200 to 500 packets in flight. The emulator's queue was nearly empty and the round trip was back at 250 ms. So congestion control wasn't holding the flows back, and neither was the emulator.

Third, the probe added the kernel's own counters. In every test, stalled or not, BBR's startup overshot hard: windows of about 15,000 packets per flow, the round trip ballooning to 733 ms, and global TCP memory reaching the kernel's `tcp_mem` pressure threshold. What separated the stalls was the receiver. It spent between 1.3 and 7 seconds under memory pressure, and the window it advertised fell to about 0.6 MB. In healthy tests it was about 100 MB. 0.6 MB is the 200 to 500 packets in flight. The path needs 31 MB.

That's a correlation, so the last step was an intervention: the same 12 tests side by side on two runners, one with the normal `tcp_mem` limits and one with four times those limits. Normal limits: 3 of 12 stalled, with the same receiver pressure and the same tiny window. Four times the limits: none of 12 stalled, all between 788 and 805 Mbit/s, and no memory-pressure events at all. Across every probe run at normal limits, 10 of 34 tests stalled, against 0 of 12 with the higher limits (one-sided Fisher exact p = 0.03).

The chain: four BBR flows overshoot during startup, total TCP memory reaches the pressure threshold, the receiver clamps its window and doesn't fully reopen it during the test, and every flow is window-limited from then on.

How much of this is the lab? Here the sender, the receiver and the emulator's queue all share one kernel's TCP memory budget, sized from the runner's 7,932 MB of RAM. Two real transfer nodes would each have their own, so they'd reach pressure later. The mechanism is real, though, and it's the opposite of the buffer lesson above. Raising socket buffers to 256 MB on a host that runs many parallel long-distance flows spends exactly the memory `tcp_mem` limits. Tune one without the other and you trade one ceiling for another. I wouldn't have predicted that from the documentation. It came out of not accepting "BBR is just noisy" as an answer.

## Monitoring that tells failures apart

The monitoring part asks a narrower question: when the fabric breaks, does monitoring notice quickly, and does it tell the right story?

The exporter publishes link state and byte counters, BGP session state, and each leaf's number of working paths to the other rack. Four alert rules sit on top: link down, BGP peer down, ECMP degraded (one path left), and prefix missing (no path left). The test breaks things on purpose. Across three CI runs:

- **A link going down** fired the link alert in 3.0 to 4.6 seconds and the BGP and ECMP alerts in 5.0 to 6.6. The prefix alert correctly stayed silent, since the other spine still carried traffic.
- **The bad route-map** fired the prefix alert in 3.5 to 5.5 seconds while every BGP alert stayed silent. That's the fabric section's worst failure, caught by the one check that looks at what the routers can actually reach, not whether they're talking.

The spread between runs isn't jitter in the network. An alert waits for the next 2-second scrape, then the next 2-second rule evaluation, then its own hold time, so detection lands on Prometheus's clock. The first two runs gave identical numbers by coincidence of timing. The third didn't.

The dashboard had its own bugs. Up links first showed red and down links green. One panel didn't render. A 6.3 Mbit/s burst showed as about 400 kb/s, because Grafana assumed points 15 seconds apart when Prometheus collected every 2. Each one was caught in a screenshot and fixed, and the next run checked the fix.

## What I'd tell an operator

1. **Check the buffer ceilings before blaming the network.** A long path with default buffers delivers a tenth of the link, with no loss and no errors to point at.
2. **On lossy long paths, congestion control is the biggest lever.** BBR held about 700 Mbit/s at 250 ms with 0.1% loss where CUBIC collapsed to about 30. Parallel streams and jumbo frames help CUBIC, but not as much.
3. **Size `tcp_mem` together with the buffers.** Large buffers and many flows push a host into TCP memory pressure, and the receiver's window is what gives.
4. **Monitor what each router can reach.** A green BGP dashboard missed a route-map mistake that took a rack offline. A prefix check caught it in seconds.

## Limits

This is an emulator on one shared cloud VM. Real paths add reordering, varying delay, bursty loss and competing traffic. The tests last 15 seconds, which favours whatever ramps up fastest. Long transfers would shrink startup effects and make CUBIC's slow recovery cost even more. The kernel's BBR is version 1. And two counters stayed unexplained, so neither is used for any claim: one BBR test with 106,267 retransmits that never recurred, and netem's own drop counter, which reports drops in tests that had zero retransmits.

The raw data for every number here is in the repository, next to the CI run that produced it.
