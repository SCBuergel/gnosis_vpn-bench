#!/bin/bash
# Operator heartbeat watchdog, installed on each client VM by gnosis_vpn-bench.
# The operator's machine touches $HB over SSH every 45 s (`heartbeat`).
#   stale > SOFT: disconnect and stop the VPN worker, drop the killswitch table
#   stale > HARD: reboot once (/run, and with it $HB, is cleared on boot)
# Usage: deadman.sh <worker-owner>   (root for v4, the per-user worker for v5)
CLI_USER=${1:?worker owner}
HB=/run/gvpn-deadman.alive
SOFT=${SOFT:-360}
HARD=${HARD:-720}
fired=0

ctl() {
    if [ "$CLI_USER" = root ]; then
        timeout 30 gnosis_vpn-ctl "$@"
    else
        timeout 30 runuser -u "$CLI_USER" -- gnosis_vpn-ctl "$@"
    fi
}

touch "$HB"
while true; do
    age=$(( $(date +%s) - $(stat -c %Y "$HB" 2>/dev/null || date +%s) ))
    if [ "$age" -gt "$HARD" ]; then
        echo "$(date -Is) heartbeat ${age}s stale, rebooting"
        sync
        systemctl reboot
        sleep 600
    elif [ "$age" -gt "$SOFT" ] && [ "$fired" -eq 0 ]; then
        echo "$(date -Is) heartbeat ${age}s stale, tearing the VPN down"
        ctl disconnect
        ctl stop-client
        sleep 5
        for fam in inet ip ip6; do
            nft delete table "$fam" gnosis_vpn_ks 2>/dev/null
        done
        fired=1
    elif [ "$age" -le "$SOFT" ]; then
        fired=0
    fi
    sleep 10
done
