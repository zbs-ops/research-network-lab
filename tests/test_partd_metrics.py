"""Part D: unit tests for partd/metrics.py. No network or etcd needed.

The log lines are real etcd v3.5.35 output (a local 3-member cluster), trimmed to the fields used.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "partd"))

from metrics import (  # noqa: E402
    check_readback, election_after, first_ack_after, leader_changes, parse_raft_events, parse_ts,
    percentile, write_stats,
)


def line(ts: str, msg: str) -> str:
    return '{"level":"info","ts":"%s","logger":"raft","caller":"etcdserver/zap_raft.go:77","msg":"%s"}' % (ts, msg)


LOG = [
    line("2026-11-08T05:43:29.601863Z", "e15554595de94448 is starting a new election at term 1"),
    line("2026-11-08T05:43:29.601954Z", "e15554595de94448 became pre-candidate at term 1"),
    line("2026-11-08T05:43:29.602493Z", "e15554595de94448 became candidate at term 2"),
    '{"level":"info","ts":"2026-11-08T05:43:29.602500Z","caller":"rafthttp/peer.go:137","msg":"started remote peer"}',
    line("2026-11-08T05:43:29.602817Z", "fac35115cd341172 became follower at term 2"),
    line("2026-11-08T05:43:29.603093Z", "e15554595de94448 became leader at term 2"),
    line("2026-11-08T05:43:29.603102Z", "raft.node: e15554595de94448 elected leader e15554595de94448 at term 2"),
    line("2026-11-08T05:43:29.603339Z", "raft.node: fac35115cd341172 elected leader e15554595de94448 at term 2"),
    "not json at all",
]


def test_parse_ts_utc_and_offset():
    assert parse_ts("1970-01-01T00:00:01.5Z") == 1.5
    assert parse_ts("1970-01-01T09:00:01.000+09:00") == 1.0
    assert parse_ts("1970-01-01T09:00:01+0900") == 1.0


def test_parse_raft_events_kinds_and_relative_time():
    t0 = parse_ts("2026-11-08T05:43:29.600000Z")
    evs = parse_raft_events(LOG, t0)
    assert [e["kind"] for e in evs] == ["election", "pre-candidate", "candidate", "follower", "leader", "elected", "elected"]
    assert abs(evs[4]["t"] - 0.003093) < 1e-6
    assert evs[4] == {"t": evs[4]["t"], "kind": "leader", "member": "e15554595de94448", "term": 2}
    assert evs[-1]["leader"] == "e15554595de94448" and evs[-1]["member"] == "fac35115cd341172"


def test_election_after_ignores_old_leader_and_counts_rounds():
    evs = [
        {"t": 0.5, "kind": "leader", "member": "aa", "term": 2},          # before the fault
        {"t": 1.2, "kind": "pre-candidate", "member": "bb", "term": 2},
        {"t": 1.2, "kind": "candidate", "member": "bb", "term": 3},
        {"t": 1.3, "kind": "candidate", "member": "cc", "term": 3},      # split vote
        {"t": 1.4, "kind": "leader", "member": "aa", "term": 3},          # old leader: not counted
        {"t": 2.1, "kind": "candidate", "member": "bb", "term": 4},
        {"t": 2.2, "kind": "leader", "member": "bb", "term": 4},
        {"t": 2.25, "kind": "elected", "member": "bb", "leader": "bb", "term": 4},
        {"t": 2.4, "kind": "elected", "member": "cc", "leader": "bb", "term": 4},
        {"t": 3.0, "kind": "candidate", "member": "cc", "term": 5},      # after: not counted
    ]
    r = election_after(evs, 1.0, "aa")
    assert r["election_s"] == 1.2 and r["new_leader"] == "bb" and r["new_term"] == 4
    assert r["candidate_rounds"] == 3 and r["pre_candidate_rounds"] == 1
    assert r["all_know_s"] == 1.4


def test_election_after_no_new_leader():
    r = election_after([{"t": 2.0, "kind": "candidate", "member": "bb", "term": 3}], 1.0, "aa")
    assert r["election_s"] is None and r["candidate_rounds"] == 1


def test_leader_changes_window():
    evs = [{"t": t, "kind": "leader", "member": "x", "term": 1} for t in (0.1, 1.0, 2.0)]
    assert leader_changes(evs, 0.5, 2.0) == 1


def test_percentile_matches_linear_interpolation():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 99) == 10
    assert percentile(list(range(101)), 99) == 99
    assert percentile([], 50) is None


def test_write_stats_gap_and_rates():
    # 10 writes at 0.1 s spacing, 10 ms latency, writes 3-6 fail: no acks from 0.21 to 0.71
    w = [[i, i / 10, i / 10 + 0.01, "ok" if not 3 <= i <= 6 else "timeout"] for i in range(10)]
    s = write_stats(w)
    assert s["sent"] == 10 and s["acked"] == 6 and s["success_rate"] == 0.6
    assert abs(s["longest_gap_s"] - 0.5) < 1e-9
    assert abs(s["p50_ms"] - 10) < 1e-6
    assert s["outcomes"] == {"ok": 6, "timeout": 4}
    assert write_stats(w, 0.25, 0.65)["acked"] == 0
    assert write_stats([], 0, 1) == {"sent": 0}


def test_write_stats_no_acks_gap_is_whole_window():
    w = [[i, i / 10, i / 10 + 2, "timeout"] for i in range(5)]
    assert abs(write_stats(w)["longest_gap_s"] - 0.4) < 1e-9


def test_first_ack_after():
    w = [[0, 0.0, 0.01, "ok"], [1, 1.0, 1.5, "timeout"], [2, 1.1, 1.3, "ok"]]
    assert abs(first_ack_after(w, 0.9) - 0.4) < 1e-9
    assert first_ack_after(w, 2.0) is None


def test_check_readback():
    acked = {"a": "1", "b": "2", "c": "3"}
    unacked = {"d": "4", "e": "5"}
    present = {"a": "1", "b": "x", "d": "4", "z": "?"}
    r = check_readback(acked, unacked, present)
    assert (r["lost"], r["wrong"], r["unacked_committed"], r["unknown_keys"]) == (1, 1, 1, 1)
    assert r["lost_keys"] == ["c"] and r["wrong_keys"] == ["b"]
