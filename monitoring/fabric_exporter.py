"""A Prometheus exporter for the Containerlab fabric, using only the standard library.

It reads each router's interface counters and link state from /sys inside the container,
its BGP sessions and routes from FRR's vtysh (JSON), and serves them on :9101/metrics.
Run as root on the lab host:  python3 monitoring/fabric_exporter.py
"""

from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

LAB = "fabric"
ROUTERS = ["spine1", "spine2", "leaf1", "leaf2"]
FABRIC_IFACES = ["eth1", "eth2"]
LEAF_PREFIXES = {"leaf1": "10.1.2.0/24", "leaf2": "10.1.1.0/24"}  # the other rack, from each leaf


def sh(node: str, *cmd: str) -> str:
    out = subprocess.run(["docker", "exec", f"clab-{LAB}-{node}", *cmd], capture_output=True, text=True, timeout=10)
    return out.stdout


def vtysh(node: str, command: str) -> dict:
    try:
        return json.loads(sh(node, "vtysh", "-c", f"{command} json") or "{}")
    except ValueError:
        return {}


def collect() -> list[str]:
    lines = [
        "# HELP fabric_interface_up 1 if the interface's operstate is up.",
        "# TYPE fabric_interface_up gauge",
        "# HELP fabric_interface_rx_bytes_total Bytes received on the interface.",
        "# TYPE fabric_interface_rx_bytes_total counter",
        "# HELP fabric_interface_tx_bytes_total Bytes sent on the interface.",
        "# TYPE fabric_interface_tx_bytes_total counter",
        "# HELP fabric_bgp_peer_established 1 if the BGP session on this interface is Established.",
        "# TYPE fabric_bgp_peer_established gauge",
        "# HELP fabric_route_nexthops Active next hops a leaf has to the other rack's prefix.",
        "# TYPE fabric_route_nexthops gauge",
    ]
    for node in ROUTERS:
        for iface in FABRIC_IFACES:
            base = f"/sys/class/net/{iface}"
            raw = sh(node, "cat", f"{base}/operstate", f"{base}/statistics/rx_bytes", f"{base}/statistics/tx_bytes").split()
            if len(raw) == 3:
                labels = f'node="{node}",iface="{iface}"'
                lines.append(f"fabric_interface_up{{{labels}}} {1 if raw[0] == 'up' else 0}")
                lines.append(f"fabric_interface_rx_bytes_total{{{labels}}} {raw[1]}")
                lines.append(f"fabric_interface_tx_bytes_total{{{labels}}} {raw[2]}")
        peers = vtysh(node, "show bgp ipv4 unicast summary").get("peers", {})
        for iface in FABRIC_IFACES:
            state = peers.get(iface, {}).get("state")
            lines.append(f'fabric_bgp_peer_established{{node="{node}",peer="{iface}"}} {1 if state == "Established" else 0}')
        if node in LEAF_PREFIXES:
            prefix = LEAF_PREFIXES[node]
            routes = vtysh(node, f"show ip route {prefix}").get(prefix, [])
            best = [r for r in routes if r.get("selected")]
            hops = sum(1 for nh in best[0]["nexthops"] if nh.get("active")) if best else 0
            lines.append(f'fabric_route_nexthops{{node="{node}",prefix="{prefix}"}} {hops}')
    return lines


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = ("\n".join(collect()) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep CI logs readable
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9101), Handler).serve_forever()
