#!/bin/sh
# OpenWrt (22.03+) — run on the router over SSH after installing:
#   opkg update && opkg install wireguard-tools luci-proto-wireguard
# Fill in the values printed by setup-vps.sh.
set -e
ROUTER_PRIVATE_KEY='...'
VPS_PUBLIC_KEY='...'
PRESHARED_KEY='...'
VPS_ENDPOINT='203.0.113.10'
PRINTER_IP='192.168.1.50'

uci set network.wg_vps=interface
uci set network.wg_vps.proto='wireguard'
uci set network.wg_vps.private_key="$ROUTER_PRIVATE_KEY"
uci add_list network.wg_vps.addresses='10.8.0.2/32'

uci add network wireguard_wg_vps
uci set network.@wireguard_wg_vps[-1].public_key="$VPS_PUBLIC_KEY"
uci set network.@wireguard_wg_vps[-1].preshared_key="$PRESHARED_KEY"
uci set network.@wireguard_wg_vps[-1].endpoint_host="$VPS_ENDPOINT"
uci set network.@wireguard_wg_vps[-1].endpoint_port='51820'
uci set network.@wireguard_wg_vps[-1].persistent_keepalive='25'
uci set network.@wireguard_wg_vps[-1].route_allowed_ips='1'
uci add_list network.@wireguard_wg_vps[-1].allowed_ips='10.8.0.1/32'

# Firewall zone for the tunnel: no masquerading, no access to the router itself.
uci add firewall zone
uci set firewall.@zone[-1].name='wgvps'
uci set firewall.@zone[-1].network='wg_vps'
uci set firewall.@zone[-1].input='REJECT'
uci set firewall.@zone[-1].output='ACCEPT'
uci set firewall.@zone[-1].forward='REJECT'

# LAN -> tunnel (printer -> bot SMTP receiver)
uci add firewall forwarding
uci set firewall.@forwarding[-1].src='lan'
uci set firewall.@forwarding[-1].dest='wgvps'

# Tunnel -> printer, IPP only (+ eSCL 8080 if SCAN_MODE=escl)
uci add firewall rule
uci set firewall.@rule[-1].name='vps-to-printer'
uci set firewall.@rule[-1].src='wgvps'
uci set firewall.@rule[-1].src_ip='10.8.0.1'
uci set firewall.@rule[-1].dest='lan'
uci set firewall.@rule[-1].dest_ip="$PRINTER_IP"
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='631 8080'
uci set firewall.@rule[-1].target='ACCEPT'

uci commit network
uci commit firewall
/etc/init.d/network reload
/etc/init.d/firewall reload
echo "done — check with: wg show"
