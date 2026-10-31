"""Small helpers to look inside the running Containerlab fabric."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Callable

LAB = "fabric"
ROUTERS = ["spine1", "spine2", "leaf1", "leaf2"]
HOSTS = {"host1": "10.1.1.10", "host2": "10.1.2.10"}


def sh(node: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command inside one lab node."""
    return subprocess.run(
        ["docker", "exec", f"clab-{LAB}-{node}", *cmd], capture_output=True, text=True, check=check, timeout=60
    )


def vtysh_json(node: str, command: str) -> Any:
    return json.loads(sh(node, "vtysh", "-c", f"{command} json").stdout or "{}")


def established_peers(node: str) -> int:
    peers = vtysh_json(node, "show bgp ipv4 unicast summary").get("peers", {})
    return sum(1 for p in peers.values() if p.get("state") == "Established")


def nexthops(node: str, prefix: str) -> int:
    """How many active next hops ``node`` has for ``prefix`` (0 if it has no route)."""
    routes = vtysh_json(node, f"show ip route {prefix}").get(prefix, [])
    best = [r for r in routes if r.get("selected")]
    return sum(1 for nh in best[0]["nexthops"] if nh.get("active")) if best else 0


def ping(src: str, dst_ip: str, count: int = 3) -> bool:
    return sh(src, "ping", "-c", str(count), "-W", "1", dst_ip, check=False).returncode == 0


def wait_for(condition: Callable[[], bool], timeout: float = 60, step: float = 0.5) -> float:
    """Poll until ``condition`` holds; return how long it took. Raise if it never does."""
    start = time.monotonic()
    while True:
        if condition():
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise TimeoutError(f"condition not met within {timeout}s")
        time.sleep(step)
