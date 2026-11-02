"""Part C: the monitoring sees what the fabric does, and alerts fire when they should.

Needs the fabric deployed, monitoring/fabric_exporter.py on :9101, and Prometheus on :9090
with monitoring/alerts.yml (see .github/workflows/monitoring.yml). Every alert's time to
fire and to clear is measured and written to results/monitoring.json.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fabric import HOSTS, ROUTERS, established_peers, nexthops, sh, wait_for

PROM = "http://localhost:9090"
RESULTS = Path(__file__).parent.parent / "results" / "monitoring.json"
records: list[dict] = []


def query(expr: str) -> list[dict]:
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]["result"]


def firing(alertname: str, **labels: str) -> bool:
    selector = ",".join([f'alertname="{alertname}"', 'alertstate="firing"'] + [f'{k}="{v}"' for k, v in labels.items()])
    return bool(query(f"ALERTS{{{selector}}}"))


def any_firing() -> list[str]:
    return sorted({r["metric"]["alertname"] for r in query('ALERTS{alertstate="firing"}')})


@pytest.fixture(scope="module", autouse=True)
def healthy():
    wait_for(lambda: all(established_peers(r) == 2 for r in ROUTERS), timeout=120)
    wait_for(lambda: bool(query("fabric_interface_up")), timeout=60)
    wait_for(lambda: not any_firing(), timeout=60)
    yield
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(records, indent=2) + "\n")
    print("MONITORING_JSON " + json.dumps(records))


@pytest.fixture(autouse=True)
def back_to_quiet():
    yield
    wait_for(lambda: nexthops("leaf1", "10.1.2.0/24") == 2 and nexthops("leaf2", "10.1.1.0/24") == 2, timeout=60)
    wait_for(lambda: not any_firing(), timeout=60)


def test_the_exporter_sees_the_whole_fabric():
    assert len(query("fabric_bgp_peer_established == 1")) == 8  # 4 routers x 2 sessions
    assert len(query("fabric_interface_up == 1")) == 8
    assert {r["metric"]["node"] for r in query("fabric_route_nexthops == 2")} == {"leaf1", "leaf2"}
    assert any_firing() == []


def test_traffic_shows_up_as_throughput():
    # 5,000 pings of 1,400 bytes, sent as fast as the 1 ms interval allows, host1 -> host2.
    sh("host1", "ping", "-q", "-c", "5000", "-i", "0.001", "-s", "1400", HOSTS["host2"], check=False)
    time.sleep(4)  # two more scrapes after the traffic ends, so the 10 s rate window holds it
    peak = max(float(r["value"][1]) for r in query('max_over_time((rate(fabric_interface_tx_bytes_total{node=~"spine.*"}[10s]) * 8)[30s:2s])'))
    records.append({"scenario": "ping traffic host1 -> host2", "peak_spine_tx_bps": round(peak)})
    assert peak > 1e6


def test_a_dead_link_raises_link_bgp_and_ecmp_alerts():
    t0 = time.monotonic()
    sh("leaf1", "ip", "link", "set", "eth1", "down")
    fired: dict[str, float] = {}
    try:
        # Each alert's time is measured from the moment the link went down.
        for name, labels in (
            ("FabricLinkDown", {"node": "leaf1", "iface": "eth1"}),
            ("BGPPeerDown", {"node": "leaf1", "peer": "eth1"}),
            ("ECMPDegraded", {"node": "leaf1"}),
        ):
            wait_for(lambda: firing(name, **labels), timeout=60)
            fired[name] = round(time.monotonic() - t0, 1)
        prefix_ok = not firing("PrefixMissing")
    finally:
        sh("leaf1", "ip", "link", "set", "eth1", "up")
    cleared = wait_for(lambda: not any_firing(), timeout=90)
    assert prefix_ok, "one link down must not look like a lost prefix"
    records.append(
        {
            "scenario": "leaf1 eth1 down",
            **{f"{k}_fired_s": v for k, v in fired.items()},
            "PrefixMissing_fired": False,
            "all_cleared_after_restore_s": round(cleared, 1),
        }
    )


def test_a_policy_mistake_raises_prefix_missing_while_bgp_stays_green():
    conf = ["route-map BLOCK deny 10", "router bgp 65102", "address-family ipv4 unicast", "neighbor FABRIC route-map BLOCK out"]
    sh("leaf2", "vtysh", "-c", "configure terminal", *[a for c in conf for a in ("-c", c)])
    try:
        fired = wait_for(lambda: firing("PrefixMissing", node="leaf1"), timeout=60)
        sessions_alerting = firing("BGPPeerDown")
    finally:
        undo = ["router bgp 65102", "address-family ipv4 unicast", "no neighbor FABRIC route-map BLOCK out"]
        sh("leaf2", "vtysh", "-c", "configure terminal", *[a for c in undo for a in ("-c", c)])
        sh("leaf2", "vtysh", "-c", "clear bgp ipv4 unicast * soft out")
    cleared = wait_for(lambda: not any_firing(), timeout=90)
    assert not sessions_alerting, "the sessions stayed up; only the prefix check should catch this"
    records.append(
        {
            "scenario": "outbound route-map denies all on leaf2",
            "PrefixMissing_fired_s": round(fired, 1),
            "BGPPeerDown_fired": False,
            "all_cleared_after_fix_s": round(cleared, 1),
        }
    )
