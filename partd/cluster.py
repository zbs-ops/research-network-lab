"""Part D: start, query and stop an etcd cluster, one member per network namespace.

Each trial gets a brand-new cluster (fresh data directories and cluster token), so a killed or
partitioned member never carries state into the next trial. Data directories are on tmpfs
(/dev/shm): the experiments are about the network, and the runner's disk fsync latency would
otherwise be mixed into every write latency.

local=True runs the members as plain processes on 127.0.1.<i> (peer) and 127.0.2.<i> (client)
instead of namespaces. That mode can't inject network faults; it exists to check the client and
log parsing on a machine without root.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import net
from metrics import parse_raft_events


class Cluster:
    def __init__(self, n: int, etcd: str, workdir: Path, heartbeat_ms: int, election_ms: int,
                 token: str, local: bool = False):
        self.n, self.etcd, self.local, self.token = n, etcd, local, token
        self.heartbeat_ms, self.election_ms = heartbeat_ms, election_ms
        self.logdir = workdir
        self.datadir = Path("/dev/shm/partd") / token
        self.procs: dict[int, subprocess.Popen] = {}
        self.ids: dict[int, str] = {}  # member index -> raft id (hex, as in the logs)

    def peer_ip(self, i: int) -> str:
        return f"127.0.1.{i}" if self.local else net.peer_ip(i)

    def client_ip(self, i: int) -> str:
        return f"127.0.2.{i}" if self.local else net.client_ip(i)

    def url(self, i: int) -> str:
        return f"http://{self.client_ip(i)}:2379"

    def start(self) -> None:
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.datadir.mkdir(parents=True, exist_ok=True)
        initial = ",".join(f"m{i}=http://{self.peer_ip(i)}:2380" for i in range(1, self.n + 1))
        for i in range(1, self.n + 1):
            cmd = [
                self.etcd, "--name", f"m{i}", "--data-dir", str(self.datadir / f"m{i}"),
                "--listen-peer-urls", f"http://{self.peer_ip(i)}:2380",
                "--initial-advertise-peer-urls", f"http://{self.peer_ip(i)}:2380",
                "--listen-client-urls", self.url(i), "--advertise-client-urls", self.url(i),
                "--initial-cluster", initial, "--initial-cluster-state", "new",
                "--initial-cluster-token", self.token,
                "--heartbeat-interval", str(self.heartbeat_ms), "--election-timeout", str(self.election_ms),
            ]
            if not self.local:
                cmd = ["ip", "netns", "exec", net.nsname(i)] + cmd
            log = open(self.logdir / f"m{i}.log", "w")
            # ip netns exec execs etcd in place, so the pid is etcd's own.
            self.procs[i] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)

    def status(self, i: int, timeout: float = 0.5) -> dict | None:
        try:
            req = urllib.request.Request(self.url(i) + "/v3/maintenance/status", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def alive(self) -> list[int]:
        return [i for i, p in self.procs.items() if p.poll() is None]

    def wait_ready(self, timeout: float) -> dict | None:
        """Wait until every live member answers and all name the same leader.

        Returns {"leader": index, "term": int, "waited_s": float} or None on timeout.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            sts = {i: self.status(i) for i in self.alive()}
            if all(sts.values()):
                for i, s in sts.items():
                    self.ids[i] = format(int(s["header"]["member_id"]), "x")
                # etcd's JSON gateway leaves out zero-valued fields, so a member with no leader
                # has no "leader" key at all (seen right after a heal in run 42452213957).
                leaders = {s.get("leader", "0") for s in sts.values()}
                if len(leaders) == 1 and leaders != {"0"}:
                    lid = format(int(leaders.pop()), "x")
                    by_id = {v: k for k, v in self.ids.items()}
                    if lid in by_id:
                        return {"leader": by_id[lid], "term": int(sts[by_id[lid]]["raftTerm"]),
                                "waited_s": round(time.time() - t0, 3)}
            time.sleep(0.05)
        return None

    def kill(self, i: int) -> None:
        self.procs[i].send_signal(signal.SIGKILL)
        self.procs[i].wait()

    def stop(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
        for p in self.procs.values():
            p.wait()
        shutil.rmtree(self.datadir, ignore_errors=True)

    def events(self, t0: float) -> list[dict]:
        out = []
        for i in range(1, self.n + 1):
            f = self.logdir / f"m{i}.log"
            if f.exists():
                for e in parse_raft_events(f.read_text(errors="replace").splitlines(), t0):
                    e["m"] = next((k for k, v in self.ids.items() if v == e["member"]), None)
                    out.append(e)
        return sorted(out, key=lambda e: e["t"])

    def log_lines(self, needle: str) -> list[str]:
        """Log lines containing needle, from every member (for warnings worth keeping)."""
        out = []
        for i in range(1, self.n + 1):
            f = self.logdir / f"m{i}.log"
            if f.exists():
                out += [f"m{i}: {line}" for line in f.read_text(errors="replace").splitlines() if needle in line]
        return out


def etcd_version(etcd: str) -> str:
    return subprocess.run([etcd, "--version"], capture_output=True, text=True).stdout.strip()


def fresh_token(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{time.time_ns()}"
