# RTT from this probe droplet to an exit node. Usage: probe-rtt.sh <ip> <count>
# Many exits ignore ICMP, so mtr's last responding hop stands in for the node.
ip="$1"
count="$2"
cloud-init status --wait >/dev/null 2>&1 || true
command -v mtr >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get -qq install -y mtr-tiny >/dev/null 2>&1 || true
echo "== ping"
ping -n -q -c "$count" -W 2 "$ip" || true
echo "== mtr"
mtr -n -r -c "$count" -w "$ip" || true
