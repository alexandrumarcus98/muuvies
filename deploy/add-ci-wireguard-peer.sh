#!/usr/bin/env bash
# Adds, or rotates, the WireGuard peer that GitHub Actions uses to reach this
# server for deploys. SSH is only open on wg0, so CI joins the VPN first.
#
#   sudo deploy/add-ci-wireguard-peer.sh [ci-address]     (default 10.0.0.250)
#
# Writes the client config (it contains a private key) to ~/muuvies-ci-wg.conf
# of the user who ran sudo, mode 600. Paste it into the DEPLOY_WG_CONFIG
# repository secret, then delete the file. Running it again replaces the old
# CI peer, which revokes the previous config.

set -euo pipefail

IFACE="${WG_IFACE:-wg0}"
CI_ADDR="${1:-${CI_WG_IP:-10.0.0.250}}"
ENDPOINT_HOST="${WG_ENDPOINT_HOST:-${WG_DOMAIN:-media.example.com}}"
CONF="${WG_CONF:-/etc/wireguard/${IFACE}.conf}"
BEGIN="# BEGIN muuvies-ci peer (managed by deploy/add-ci-wireguard-peer.sh)"
END="# END muuvies-ci peer"

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
command -v wg >/dev/null || die "wireguard-tools is not installed."
wg show "${IFACE}" >/dev/null 2>&1 || die "WireGuard interface ${IFACE} is not up."
[ -f "${CONF}" ] || die "${CONF} not found."

OWNER="${SUDO_USER:-root}"
OUT="$(getent passwd "${OWNER}" | cut -d: -f6)/muuvies-ci-wg.conf"

server_cidr="$(ip -4 -o addr show dev "${IFACE}" | awk '{print $4; exit}')"
server_ip="${server_cidr%/*}"
python3 - "${server_cidr}" "${CI_ADDR}" <<'EOF' || die "${CI_ADDR} is not a host address inside ${server_cidr}."
import ipaddress, sys
net = ipaddress.ip_interface(sys.argv[1]).network
ip = ipaddress.ip_address(sys.argv[2])
sys.exit(0 if ip in net and ip not in (net.network_address, net.broadcast_address) and str(ip) != sys.argv[1].split("/")[0] else 1)
EOF
port="$(wg show "${IFACE}" listen-port)"
server_pub="$(wg show "${IFACE}" public-key)"

# Previous CI peer, if any (identified by the managed block in the config).
old_pub="$(awk -v b="${BEGIN}" -v e="${END}" '$0==b{f=1;next} $0==e{f=0} f && $1=="PublicKey"{print $3}' "${CONF}")"

# With SaveConfig = true, wg-quick rewrites the file from the live state on shutdown,
# so the live peer is what persists and the managed block is not written.
save_config=false
grep -qiE '^[[:space:]]*SaveConfig[[:space:]]*=[[:space:]]*true' "${CONF}" && save_config=true

# Refuse to take an address another (non-CI) peer already uses.
while read -r pub ips; do
    [ "${pub}" = "${old_pub}" ] && continue
    for ip in ${ips//,/ }; do
        [ "${ip}" = "${CI_ADDR}/32" ] && die "${CI_ADDR} is already used by peer ${pub}. Pass another address, or if it is an old CI peer remove it first: wg set ${IFACE} peer ${pub} remove"
    done
done < <(wg show "${IFACE}" allowed-ips)

umask 077
priv="$(wg genkey)"
pub="$(printf '%s' "${priv}" | wg pubkey)"
psk="$(wg genpsk)"

cp -p "${CONF}" "${CONF}.bak-$(date +%Y%m%d-%H%M%S)"

if [ -n "${old_pub}" ]; then
    wg set "${IFACE}" peer "${old_pub}" remove
    tmp="$(mktemp)"
    awk -v b="${BEGIN}" -v e="${END}" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "${CONF}" > "${tmp}"
    cat "${tmp}" > "${CONF}" && rm -f "${tmp}"
    echo "Removed the previous CI peer."
fi

wg set "${IFACE}" peer "${pub}" preshared-key <(printf '%s\n' "${psk}") allowed-ips "${CI_ADDR}/32"
[ "${save_config}" = true ] || printf '\n%s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n%s\n' \
    "${BEGIN}" "${pub}" "${psk}" "${CI_ADDR}" "${END}" >> "${CONF}"

cat > "${OUT}" <<EOF
[Interface]
PrivateKey = ${priv}
Address = ${CI_ADDR}/32

[Peer]
PublicKey = ${server_pub}
PresharedKey = ${psk}
Endpoint = ${ENDPOINT_HOST}:${port}
AllowedIPs = ${server_ip}/32
PersistentKeepalive = 25
EOF
chown "${OWNER}:" "${OUT}"
chmod 600 "${OUT}"

echo "CI peer ${CI_ADDR} is live on ${IFACE} and saved in ${CONF} (backup alongside)."
echo "Client config: ${OUT}"
echo "  1. Paste its contents into the GitHub repository secret DEPLOY_WG_CONFIG."
echo "  2. Delete it: rm ${OUT}"
