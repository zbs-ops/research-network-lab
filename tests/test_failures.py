"""Break the fabric on purpose and measure how it notices and recovers.

Each scenario records what was broken, how long until routing reacted, how many pings a
host lost meanwhile, and what the operator would see while diagnosing it. Results go to
results/failures.json so the write-up quotes measured numbers, not expectations.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from fabric import HOSTS, LAB, ROUTERS, established_peers, nexthops, ping, sh, wait_for

RESULTS = Path(__file__).parent.parent / "results" / "failures.json"
FAR = "10.1.2.0/24"  # host2's rack, as seen from leaf1
records: list[dict] = []


def pid(node: str) -> str:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", f"clab-{LAB}-{node}"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def netns(node: str, *cmd: str) -> None:
    """Run a host tool (like tc) inside a node's network namespace."""
    subprocess.run(["nsenter", "-t", pid(node), "-n", *cmd], check=True)


class PingMeter:
    """host1 pings host2 every 100 ms in the background while a scenario runs."""

    def __init__(self, count: int = 150):
        self.proc = subprocess.Popen(
            ["docker", "exec", f"clab-{LAB}-host1", "ping", "-i", "0.1", "-c", str(count), HOSTS["host2"]],
            stdout=subprocess.PIPE,
            text=True,
        )

    def lost(self) -> int:
        out, _ = self.proc.communicate(timeout=60)
        m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", out)
        assert m, out
        return int(m.group(1)) - int(m.group(2))


@pytest.fixture(scope="module", autouse=True)
def healthy_fabric():
    wait_for(lambda: all(established_peers(r) == 2 for r in ROUTERS), timeout=120)
    wait_for(lambda: nexthops("leaf1", FAR) == 2, timeout=30)
    yield
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(records, indent=2) + "\n")
    print("FAILURES_JSON " + json.dumps(records))


@pytest.fixture(autouse=True)
def back_to_healthy():
    yield
    # Every scenario must leave the fabric as it found it, or the next one measures the wrong thing.
    wait_for(lambda: all(established_peers(r) == 2 for r in ROUTERS), timeout=60)
    wait_for(lambda: nexthops("leaf1", FAR) == 2 and nexthops("leaf2", "10.1.1.0/24") == 2, timeout=60)


def test_spine_down():
    """Both of spine1's links go dark, as if it lost power."""
    meter = PingMeter()
    time.sleep(1)
    t0 = time.monotonic()
    for i in ("eth1", "eth2"):
        sh("spine1", "ip", "link", "set", i, "down")
    detect = wait_for(lambda: nexthops("leaf1", FAR) == 1, timeout=30)
    assert ping("host1", HOSTS["host2"]), "traffic should keep flowing through spine2"
    time.sleep(2)
    for i in ("eth1", "eth2"):
        sh("spine1", "ip", "link", "set", i, "up")
    t1 = time.monotonic()
    recover = wait_for(lambda: nexthops("leaf1", FAR) == 2, timeout=60)
    records.append(
        {
            "scenario": "spine down (links dark)",
            "detected_s": round(detect, 2),
            "recovered_s": round(recover, 2),
            "pings_lost_of_150": meter.lost(),
            "seen_by_operator": "leaf1 drops to one path for 10.1.2.0/24; BGP sessions to spine1 go Idle",
            "since_failure_s": round(time.monotonic() - t0, 1),
            "since_restore_s": round(time.monotonic() - t1, 1),
        }
    )


def test_silent_link_failure_caught_by_bfd():
    """The link stays up but drops every packet (a bad optic, a broken middlebox).
    Nothing changes at the interface, so only BFD (or the BGP hold timer) can notice."""
    meter = PingMeter()
    time.sleep(1)
    netns("spine1", "tc", "qdisc", "add", "dev", "eth1", "root", "netem", "loss", "100%")
    try:
        detect = wait_for(lambda: nexthops("leaf1", FAR) == 1, timeout=30, step=0.1)
        link_state = sh("spine1", "cat", "/sys/class/net/eth1/operstate").stdout.strip()
    finally:
        netns("spine1", "tc", "qdisc", "del", "dev", "eth1", "root")
    recover = wait_for(lambda: nexthops("leaf1", FAR) == 2, timeout=60)
    assert link_state == "up", "the point of this scenario is that the link never went down"
    assert detect < 3, "BFD should catch it well before the 9 s BGP hold timer"
    records.append(
        {
            "scenario": "silent link failure (100% loss, link up)",
            "detected_s": round(detect, 2),
            "recovered_s": round(recover, 2),
            "pings_lost_of_150": meter.lost(),
            "seen_by_operator": f"interface operstate still '{link_state}'; BFD peer down, then BGP session down on eth1",
        }
    )


def test_link_flap():
    """leaf1's link to spine1 bounces three times in a row."""
    meter = PingMeter()
    time.sleep(1)
    for _ in range(3):
        sh("leaf1", "ip", "link", "set", "eth1", "down")
        time.sleep(1)
        sh("leaf1", "ip", "link", "set", "eth1", "up")
        time.sleep(1)
    recover = wait_for(lambda: nexthops("leaf1", FAR) == 2, timeout=60)
    records.append(
        {
            "scenario": "link flap x3 (leaf1-spine1)",
            "detected_s": None,
            "recovered_s": round(recover, 2),
            "pings_lost_of_150": meter.lost(),
            "seen_by_operator": "BGP session on leaf1 eth1 resets three times; the path through spine2 never moves",
        }
    )


def test_bgp_misconfig_stops_announcements():
    """A bad outbound route-map on leaf2: sessions stay up, but leaf2 stops announcing its rack.
    The classic 'everything is green and nothing works' failure."""
    conf = ["route-map BLOCK deny 10", "router bgp 65102", "address-family ipv4 unicast", "neighbor FABRIC route-map BLOCK out"]
    sh("leaf2", "vtysh", "-c", "configure terminal", *[a for c in conf for a in ("-c", c)])
    try:
        detect = wait_for(lambda: nexthops("leaf1", FAR) == 0, timeout=30)
        sessions_up = established_peers("leaf2")
        reachable = ping("host1", HOSTS["host2"], count=2)
        advertised = sh("leaf2", "vtysh", "-c", "show bgp ipv4 unicast neighbors eth1 advertised-routes").stdout
    finally:
        undo = ["router bgp 65102", "address-family ipv4 unicast", "no neighbor FABRIC route-map BLOCK out"]
        sh("leaf2", "vtysh", "-c", "configure terminal", *[a for c in undo for a in ("-c", c)])
        sh("leaf2", "vtysh", "-c", "clear bgp ipv4 unicast * soft out")
    recover = wait_for(lambda: nexthops("leaf1", FAR) == 2, timeout=60)
    assert sessions_up == 2 and not reachable
    records.append(
        {
            "scenario": "BGP misconfig (outbound route-map denies all on leaf2)",
            "detected_s": round(detect, 2),
            "recovered_s": round(recover, 2),
            "pings_lost_of_150": None,
            "seen_by_operator": f"both leaf2 sessions still Established ({sessions_up}/2) but leaf1 has no route to "
            f"10.1.2.0/24; leaf2 advertised-routes shows {'nothing' if '10.1.2.0/24' not in advertised else 'the prefix'}",
        }
    )
