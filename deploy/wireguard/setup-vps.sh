#!/usr/bin/env bash
# Creates the WireGuard server on the VPS and prints the config for the HOME ROUTER.
#
#   sudo ./deploy/wireguard/setup-vps.sh <home-lan-cidr> [vps-public-ip]
#   e.g. sudo ./deploy/wireguard/setup-vps.sh 192.168.1.0/24
#
# Tunnel addressing (matches .env.example):
#   10.8.0.1  VPS
#   10.8.0.2  home router
set -euo pipefail

HOME_LAN="${1:?usage: $0 <home-lan-cidr> [vps-public-ip]}"
VPS_IP="${2:-$(curl -4 -fsS https://ifconfig.me || true)}"
PORT=51820
CONF=/etc/wireguard/wg0.conf

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
[[ -e $CONF ]] && { echo "$CONF already exists; refusing to overwrite"; exit 1; }
[[ -n $VPS_IP ]] || { echo "could not detect the VPS public IP; pass it as 2nd argument"; exit 1; }

command -v wg >/dev/null || { apt-get update && apt-get install -y wireguard; }

umask 077
VPS_PRIV=$(wg genkey);    VPS_PUB=$(echo "$VPS_PRIV" | wg pubkey)
ROUTER_PRIV=$(wg genkey); ROUTER_PUB=$(echo "$ROUTER_PRIV" | wg pubkey)
PSK=$(wg genpsk)

cat > "$CONF" <<CONF
[Interface]
Address = 10.8.0.1/24
ListenPort = $PORT
PrivateKey = $VPS_PRIV
# Anything addressed to the tunnel IP (e.g. the bot's SMTP receiver) must arrive via wg0.
PostUp = iptables -t raw -I PREROUTING -d 10.8.0.1 ! -i wg0 -j DROP
PreDown = iptables -t raw -D PREROUTING -d 10.8.0.1 ! -i wg0 -j DROP

[Peer]
# home router
PublicKey = $ROUTER_PUB
PresharedKey = $PSK
# Router tunnel IP + the home LAN (so the bot can reach the printer and the
# printer can reach the bot's SMTP receiver).
AllowedIPs = 10.8.0.2/32, $HOME_LAN
CONF

systemctl enable --now wg-quick@wg0

# Docker must start after wg0 exists, because the SMTP port is bound to 10.8.0.1.
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/10-after-wireguard.conf <<UNIT
[Unit]
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
UNIT
systemctl daemon-reload

cat <<ROUTER

=====================================================================
 WireGuard config for the HOME ROUTER  (keep it secret!)
=====================================================================
[Interface]
PrivateKey = $ROUTER_PRIV
Address = 10.8.0.2/32

[Peer]
PublicKey = $VPS_PUB
PresharedKey = $PSK
Endpoint = $VPS_IP:$PORT
# ONLY the VPS tunnel address. Never 0.0.0.0/0 — that would send all home
# Internet traffic through the VPS.
AllowedIPs = 10.8.0.1/32
PersistentKeepalive = 25
=====================================================================
Router firewall: allow 10.8.0.1 -> printer TCP 631 (and 8080 for eSCL),
deny everything else from the tunnel to the LAN. See README.
ROUTER
