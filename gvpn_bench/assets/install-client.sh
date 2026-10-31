#!/bin/bash
# Install a pinned Gnosis VPN package and start its worker.
# Run as root on a client VM by `gnosis_vpn-bench provision`.
#
# Usage: install-client.sh CHANNEL NETWORK INDEXER PKG_URL PKG_SHA256 PKG_VERSION \
#                          WORKER_KIND WORKER_USER WORKER_HOME
#   WORKER_KIND is v4-root (root-owned worker) or v5-user (per-user worker).
set -euo pipefail
CHANNEL=$1 NETWORK=$2 INDEXER=$3 PKG_URL=$4 PKG_SHA256=$5 PKG_VERSION=$6
WORKER_KIND=$7 WORKER_USER=$8 WORKER_HOME=$9
WORK=/root/gvpn-bench
export DEBIAN_FRONTEND=noninteractive
mkdir -p "$WORK"

# The official installer sets up the apt source and service for the channel.
curl -fsSL https://download.vpn.gnosis.eth.limo/linux/install.sh -o "$WORK/install.sh"
bash "$WORK/install.sh" --channel="$CHANNEL" --network="$NETWORK" > "$WORK/install.log" 2>&1

# Then pin the exact package: verify its hash before installing it.
curl -fsSL "$PKG_URL" -o "$WORK/pinned.deb"
printf '%s  %s\n' "$PKG_SHA256" "$WORK/pinned.deb" | sha256sum -c -
GNOSISVPN_NETWORK="$NETWORK" GNOSISVPN_HOPR_BLOKLI_URL="$INDEXER" \
    apt-get install -y --allow-downgrades -o Dpkg::Options::=--force-confold "$WORK/pinned.deb" \
    > "$WORK/pin.log" 2>&1
test "$(dpkg-query -W -f='${Version}' gnosisvpn)" = "$PKG_VERSION"

if [ "$WORKER_KIND" = v5-user ]; then
    # v5 workers are per-user and cannot be owned by root. Builds up to
    # 2026.10.28 ship a "gnosisvpn" group for them; from 2026.10.31 root's
    # service spawns the worker as any requesting user and there is no group.
    if getent group gnosisvpn >/dev/null; then
        GROUP_ARGS=(--gid gnosisvpn)
    else
        GROUP_ARGS=(--user-group)
    fi
    if ! id "$WORKER_USER" >/dev/null 2>&1; then
        useradd --create-home --home-dir "$WORKER_HOME" "${GROUP_ARGS[@]}" \
            --shell /usr/sbin/nologin "$WORKER_USER"
    fi
    test "$(id -u "$WORKER_USER")" -ne 0
    test "$(getent passwd "$WORKER_USER" | cut -d: -f6)" = "$WORKER_HOME"
    ctl() { runuser -u "$WORKER_USER" -- gnosis_vpn-ctl "$@"; }
elif [ "$WORKER_KIND" = v4-root ]; then
    ctl() { gnosis_vpn-ctl "$@"; }
else
    echo "unknown worker kind $WORKER_KIND" >&2
    exit 2
fi

gnosis_vpn-ctl --version
dpkg-query -W gnosisvpn

for _ in $(seq 1 30); do
    RESPONSE=$(ctl -o json start-client 30m) || true
    if printf '%s' "$RESPONSE" | jq -e '.StartClient == "Started" or .StartClient == "AlreadyRunning"' >/dev/null; then
        echo "worker started"
        exit 0
    fi
    sleep 2
done
echo "worker did not start: $RESPONSE" >&2
exit 1
