#!/bin/sh
# Part D: print what the runner supports before the Raft experiments rely on it.
# Like check_runner.sh, every check prints its own evidence and the script fails if one is missing.
# Usage: sh scripts/check_partd.sh <path to etcd binary dir>
set -u
ETCD_DIR=${1:-/usr/local/bin}
fail=0
check() { name=$1; shift; if "$@" >/tmp/check.out 2>&1; then echo "OK    $name"; else echo "MISSING $name"; fail=1; fi; sed 's/^/      /' /tmp/check.out | head -8; }

echo "== date (UTC)"; date -u +%Y-%m-%dT%H:%M:%SZ
echo "== kernel"; uname -r
echo "== cpus"; nproc
echo "== memory (MB)"; free -m | sed -n 2p
check "root via sudo"     sudo -n true
check "NET_ADMIN (CapEff of a root shell)" sh -c 'sudo grep CapEff /proc/self/status && sudo ip link add pdchk0 type dummy && sudo ip link del pdchk0 && echo "created and deleted a link"'
check "tc (iproute2)"     tc -V
check "iptables"          sudo iptables -V
check "network namespaces" sh -c 'sudo ip netns add pdchk && sudo ip netns exec pdchk ip link set lo up && sudo ip netns exec pdchk ip -br link; r=$?; sudo ip netns del pdchk; exit $r'
check "bridge + veth into a namespace" sh -c '
  sudo ip netns add pdchk && sudo ip link add pdbr type bridge && sudo ip link set pdbr up &&
  sudo ip link add pdv0 type veth peer name pdv1 && sudo ip link set pdv1 netns pdchk &&
  sudo ip link set pdv0 master pdbr && sudo ip link set pdv0 up &&
  sudo ip addr add 10.99.0.1/24 dev pdbr &&
  sudo ip netns exec pdchk ip addr add 10.99.0.2/24 dev pdv1 && sudo ip netns exec pdchk ip link set pdv1 up &&
  ping -c 2 -W 1 10.99.0.2; r=$?
  sudo ip link del pdv0; sudo ip link del pdbr; sudo ip netns del pdchk; exit $r'
check "netem delay+loss inside a namespace" sh -c '
  sudo ip netns add pdchk && sudo ip netns exec pdchk ip link add d0 type dummy && sudo ip netns exec pdchk ip link set d0 up &&
  sudo ip netns exec pdchk tc qdisc add dev d0 root netem delay 50ms loss 5% &&
  sudo ip netns exec pdchk tc qdisc show dev d0; r=$?; sudo ip netns del pdchk; exit $r'
check "iptables DROP rules inside a namespace" sh -c '
  sudo ip netns add pdchk && sudo ip netns exec pdchk iptables -I INPUT -s 10.99.0.9 -j DROP &&
  sudo ip netns exec pdchk iptables -S INPUT; r=$?
  n=$(sudo iptables -S INPUT | grep -c 10.99.0.9); echo "same rule in the root namespace: $n (must be 0)"
  sudo ip netns del pdchk; [ $r = 0 ] && [ "$n" = 0 ]'
check "etcd binary"       "$ETCD_DIR/etcd" --version
check "etcdctl binary"    "$ETCD_DIR/etcdctl" version
check "python3"           python3 --version
exit $fail
