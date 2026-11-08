# Part D: Raft under network faults

**Question:** research networks run replicated control-plane services (configuration stores, schedulers, Kubernetes) on Raft. When the network between the replicas misbehaves (a slow or lossy path, a dead node, a cut link), how long is there no leader, how much of a steady write load fails, which side of a split keeps working, and is any acknowledged write lost?

**Setup** (`partd/`, run in GitHub Actions by `.github/workflows/partd.yml`):

- **etcd v3.5.35**, the official release binary, downloaded in CI and checked against the release's `SHA256SUMS`. Clusters of **3 and 5 members**.
- **One Linux network namespace per member**, each with two interfaces on two bridges: a peer interface for Raft traffic and a client interface for the test client (`partd/net.py`). Faults only touch the peer side, so the client's own path stays clean and every outage the client sees comes from Raft.
- **Delay and loss:** `tc netem` on every member's peer interface, egress. A packet between two members crosses one netem, so "50 ms" is the one-way delay between any two members (100 ms round trip) and "5%" is the loss in each direction. The grid is every combination of 0, 10, 50 and 100 ms with 0, 1, 5 and 10% loss.
- **Faults:** the leader is **killed** (`SIGKILL`) or **isolated** (two `iptables DROP` rules on its peer interface, inside its namespace). DROP rather than REJECT, so connections hang as in a real cut instead of failing fast. **Partitions** are `DROP` rules by peer address in every member's namespace, with the old leader on the minority or the majority side.
- **Timeouts:** etcd's defaults (heartbeat 100 ms, election timeout 1,000 ms) and one tuned pair (50 ms / 500 ms). etcd 3.5 has pre-vote on by default (its startup log says `"pre-vote":true`).
- **The client** (`partd/client.py`) writes a unique key at a fixed 50 writes/s, open loop (write k is sent at k/50 s whatever happened to earlier writes), one HTTP request per write to etcd's JSON gateway, with a 2 s client timeout and no retries. In the election experiment it writes to a follower that stays healthy. In the partition experiment one client writes to each side.
- **Every trial is a fresh cluster:** new data directories (on tmpfs, so disk fsync latency doesn't mix into the network results) and a new cluster token. A trial is a few seconds of writes (3 s in the first run, 8 s in the second), the fault, 8 s (election) or 10 s (partition) until the heal, then 4–6 s more. Isolations and partitions are then healed; a killed leader stays dead.
- **After every trial**, a linearizable range read returns every key the client wrote, and each acknowledged key is checked for presence and value.
- **Election time comes from etcd's own log:** the time from the fault to the first `became leader` by another member, and the number of elections (`became candidate`) that took.
- Repetition is the outer loop, so drift on a runner spreads across all configurations. Each of the ten jobs (cluster size × timeout pair × fault, plus one partition job per size) runs on its own runner.

**Runs used.** Two full runs, each with 5 repetitions of every election configuration and partition trials. Every number below is median [min–max] over both runs together, with the run links next to it:

- [run 42452414571](https://github.com/zbs-ops/research-network-lab/actions/runs/42452414571): 640 election trials and 20 partition trials per cluster size at 0 ms. Raw data: `results/partd/run-42452414571/`.
- [run 42454644849](https://github.com/zbs-ops/research-network-lab/actions/runs/42454644849): the same 640 election trials with an 8 s pre-fault phase, and 40 partition trials per size at 0 and 50 ms, with `ss` snapshots of the peer connections around each heal. Raw data: `results/partd/run-42454644849/`.
- [run 42452213957](https://github.com/zbs-ops/research-network-lab/actions/runs/42452213957) was a one-repetition debug run. It found two bugs (see "What broke") and none of its numbers are used.

The summaries (`results/partd/election_summary.csv`, `partition_summary.csv`, `trials.csv`) and the figures are rebuilt from the committed trial files by `partd/analyze.py` and `partd/plot.py`; the `partd-plots` workflow does this in CI and checks the result matches what's committed. The tables below are generated from those CSVs by `partd/tables.py`.

**Environment check.** Before measuring, each job runs `scripts/check_partd.sh` and its output is committed next to the data (`env-check.txt` in each job's directory). On every runner in both runs it found kernel `7.0.0-1017-azure`, 4 CPUs, 15,982–15,983 MB of RAM (`free -m`), a root shell with full capabilities (`CapEff: 000001ffffffffff`), `iproute2-6.1.0`, `iptables v1.8.10 (nf_tables)`, a working bridge + veth path into a namespace, a netem qdisc with delay and loss inside a namespace, an `iptables` rule in a namespace that doesn't appear in the root namespace, and `etcd Version: 3.5.35`.
