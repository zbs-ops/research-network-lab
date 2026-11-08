"""Part D network: one Linux network namespace per etcd member, two bridges.

    peer bridge rdpeer  10.20.0.0/24   Raft traffic between members (faults go here)
    client bridge rdcli 10.21.0.0/24   the test client (root namespace, 10.21.0.254) to each member

Member i lives in namespace rd<i> with two interfaces: p0 (10.20.0.<i>) on the peer bridge and
c0 (10.21.0.<i>) on the client bridge. Faults touch only p0 and peer addresses, so the client's
own path stays clean and every client-side outage comes from Raft, not from the client's link.

- delay/loss: tc netem on each member's p0, egress. A packet from member a to member b crosses
  exactly one netem (a's), so the one-way delay between members is the configured delay and
  each direction loses packets at the configured rate.
- partitions: iptables DROP rules inside the member namespaces, by peer address. DROP (not
  REJECT) so connections hang like a real partition instead of failing fast.

Every function returns or logs the commands' real output, so the trial record shows what was applied.
"""

from __future__ import annotations

import subprocess

PEER_BR, CLI_BR = "rdpeer", "rdcli"
CLIENT_IP = "10.21.0.254"


def peer_ip(i: int) -> str:
    return f"10.20.0.{i}"


def client_ip(i: int) -> str:
    return f"10.21.0.{i}"


def nsname(i: int) -> str:
    return f"rd{i}"


def run(*cmd: str, check: bool = True) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout


def ns(i: int, *cmd: str, check: bool = True) -> str:
    return run("ip", "netns", "exec", nsname(i), *cmd, check=check)


def teardown(n_max: int = 9) -> None:
    for i in range(1, n_max + 1):
        run("ip", "netns", "del", nsname(i), check=False)
        run("ip", "link", "del", f"rdp{i}", check=False)
        run("ip", "link", "del", f"rdc{i}", check=False)
    run("ip", "link", "del", PEER_BR, check=False)
    run("ip", "link", "del", CLI_BR, check=False)


def setup(n: int) -> dict:
    """Build n member namespaces and both bridges. Returns what the host reported."""
    teardown()
    notes = {}
    # Docker on the runner loads br_netfilter and sets the FORWARD policy to DROP, which would
    # also drop frames crossing our bridges. Bridged frames shouldn't go through the host's
    # iptables at all here; the partitions are applied inside the member namespaces instead.
    notes["bridge-nf-call-iptables before"] = run(
        "sh", "-c", "cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo 'not loaded'").strip()
    run("sh", "-c", "[ -e /proc/sys/net/bridge/bridge-nf-call-iptables ] && "
        "sysctl -qw net.bridge.bridge-nf-call-iptables=0 || true")
    for br in (PEER_BR, CLI_BR):
        run("ip", "link", "add", br, "type", "bridge")
        run("ip", "link", "set", br, "up")
    run("ip", "addr", "add", f"{CLIENT_IP}/24", "dev", CLI_BR)
    for i in range(1, n + 1):
        run("ip", "netns", "add", nsname(i))
        ns(i, "ip", "link", "set", "lo", "up")
        for host_end, ns_end, br, ip in ((f"rdp{i}", "p0", PEER_BR, peer_ip(i)),
                                         (f"rdc{i}", "c0", CLI_BR, client_ip(i))):
            run("ip", "link", "add", host_end, "type", "veth", "peer", "name", ns_end, "netns", nsname(i))
            run("ip", "link", "set", host_end, "master", br)
            run("ip", "link", "set", host_end, "up")
            ns(i, "ip", "addr", "add", f"{ip}/24", "dev", ns_end)
            ns(i, "ip", "link", "set", ns_end, "up")
    notes["members"] = n
    return notes


def shape(n: int, delay_ms: float, loss_pct: float) -> list[str]:
    """netem on every member's peer interface. Returns `tc qdisc show` for each member."""
    shown = []
    for i in range(1, n + 1):
        ns(i, "tc", "qdisc", "del", "dev", "p0", "root", check=False)
        args = []
        if delay_ms:
            args += ["delay", f"{delay_ms}ms"]
        if loss_pct:
            args += ["loss", f"{loss_pct}%"]
        if args:
            ns(i, "tc", "qdisc", "add", "dev", "p0", "root", "netem", *args)
        shown.append(f"rd{i}: " + ns(i, "tc", "qdisc", "show", "dev", "p0").strip())
    return shown


def partition(n: int, side_a: list[int], side_b: list[int]) -> list[str]:
    """Drop all peer traffic between the two sides (both directions, in every member)."""
    applied = []
    for mine, other in ((side_a, side_b), (side_b, side_a)):
        for i in mine:
            for j in other:
                ns(i, "iptables", "-I", "INPUT", "-s", peer_ip(j), "-j", "DROP")
                ns(i, "iptables", "-I", "OUTPUT", "-d", peer_ip(j), "-j", "DROP")
            applied.append(f"rd{i}: drop to/from " + ",".join(peer_ip(j) for j in other))
    return applied


def isolate(n: int, i: int) -> list[str]:
    """Cut member i off from every other member. The client can still reach it.

    Two rules on member i's peer interface, so the cut takes effect at once (per-address rules
    in every namespace take 4(n-1) iptables calls, tens of milliseconds, to apply).
    """
    ns(i, "iptables", "-I", "INPUT", "-i", "p0", "-j", "DROP")
    ns(i, "iptables", "-I", "OUTPUT", "-o", "p0", "-j", "DROP")
    return [f"rd{i}: drop everything in and out of p0"]


def heal(n: int) -> None:
    for i in range(1, n + 1):
        ns(i, "iptables", "-F", "INPUT")
        ns(i, "iptables", "-F", "OUTPUT")


def rules(n: int) -> list[str]:
    return [f"rd{i}: " + " | ".join(ns(i, "iptables", "-S").split("\n")).strip(" |") for i in range(1, n + 1)]
