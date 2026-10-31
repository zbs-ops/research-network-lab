#!/bin/sh
# Print what this machine actually supports before the lab relies on it.
# Every check prints its own evidence; the script fails if a required one is missing.
set -u
fail=0
check() { name=$1; shift; if "$@" >/tmp/check.out 2>&1; then echo "OK    $name"; else echo "MISSING $name"; fail=1; fi; sed 's/^/      /' /tmp/check.out | head -5; }

echo "== kernel"; uname -r
check "docker"            docker version --format 'server {{.Server.Version}}'
check "tc (iproute2)"     tc -V
check "root via sudo"     sudo -n true
check "netem qdisc"       sh -c 'sudo ip link add gbbchk type dummy && sudo ip link set gbbchk up && sudo tc qdisc add dev gbbchk root netem delay 50ms loss 1% && sudo tc qdisc show dev gbbchk; r=$?; sudo ip link del gbbchk; exit $r'
check "network namespaces (NET_ADMIN)" sh -c 'sudo ip netns add gbbchk && sudo ip netns exec gbbchk ip link set lo up && sudo ip netns exec gbbchk ip -br link; r=$?; sudo ip netns del gbbchk; exit $r'
check "TCP BBR available" sh -c 'sudo modprobe tcp_bbr 2>/dev/null; cat /proc/sys/net/ipv4/tcp_available_congestion_control | grep -w bbr'
check "jumbo MTU 9000 on veth" sh -c 'sudo ip link add gbbva type veth peer name gbbvb && sudo ip link set gbbva mtu 9000 && ip link show gbbva | grep -o "mtu 9000"; r=$?; sudo ip link del gbbva; exit $r'
exit $fail
