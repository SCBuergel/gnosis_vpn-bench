#!/bin/bash
# Keep the operator's SSH session alive while the VPN replaces routes and
# installs its killswitch. Installed on each client VM by gnosis_vpn-bench.
# Usage: ssh-keeper.sh <operator-ip> <wan-gateway> <wan-device>
MYIP=${1:?operator ip}
GW=${2:?wan gateway}
DEV=${3:?wan device}

while true; do
    ip route replace "$MYIP/32" via "$GW" dev "$DEV" 2>/dev/null
    for fam in inet ip ip6; do
        if nft list table "$fam" gnosis_vpn_ks >/dev/null 2>&1; then
            if ! nft list chain "$fam" gnosis_vpn_ks input 2>/dev/null | grep -Fq "$MYIP"; then
                nft insert rule "$fam" gnosis_vpn_ks input ip saddr "$MYIP" accept 2>/dev/null
                nft insert rule "$fam" gnosis_vpn_ks output ip daddr "$MYIP" accept 2>/dev/null
                echo "$(date -Is) inserted killswitch allow for $MYIP ($fam)"
            fi
        fi
    done
    sleep 1
done
