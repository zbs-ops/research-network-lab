"""The fabric works the way the design says it should, checked on the running lab."""

import pytest

from fabric import HOSTS, ROUTERS, established_peers, nexthops, ping, wait_for


@pytest.fixture(scope="module", autouse=True)
def converged():
    # Every router has two fabric links, so two BGP sessions each once the fabric is up.
    wait_for(lambda: all(established_peers(r) == 2 for r in ROUTERS), timeout=120)


@pytest.mark.parametrize("router", ROUTERS)
def test_every_router_has_both_bgp_sessions_up(router):
    assert established_peers(router) == 2


@pytest.mark.parametrize("leaf, prefix", [("leaf1", "10.1.2.0/24"), ("leaf2", "10.1.1.0/24")])
def test_each_leaf_reaches_the_other_rack_through_both_spines(leaf, prefix):
    # ECMP: one path through each spine.
    wait_for(lambda: nexthops(leaf, prefix) == 2, timeout=30)
    assert nexthops(leaf, prefix) == 2


@pytest.mark.parametrize("spine", ["spine1", "spine2"])
@pytest.mark.parametrize("prefix", ["10.1.1.0/24", "10.1.2.0/24"])
def test_each_spine_knows_every_rack(spine, prefix):
    assert nexthops(spine, prefix) == 1


def test_hosts_in_different_racks_can_talk():
    assert ping("host1", HOSTS["host2"])
    assert ping("host2", HOSTS["host1"])
