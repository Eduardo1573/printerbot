# MikroTik RouterOS 7 — paste into the terminal, after replacing the values
# printed by setup-vps.sh. Assumes the LAN bridge is "bridge".
:local routerPrivateKey "..."
:local vpsPublicKey "..."
:local presharedKey "..."
:local vpsEndpoint "203.0.113.10"
:local printerIp "192.168.1.50"

/interface wireguard add name=wg-vps listen-port=13231 private-key=$routerPrivateKey
/interface wireguard peers add interface=wg-vps public-key=$vpsPublicKey preshared-key=$presharedKey \
    endpoint-address=$vpsEndpoint endpoint-port=51820 allowed-address=10.8.0.1/32 persistent-keepalive=25s
/ip address add address=10.8.0.2/24 interface=wg-vps

# Tunnel -> printer: IPP (631) and eSCL (8080) only. Place these BEFORE any drop rules.
/ip firewall filter add chain=forward in-interface=wg-vps src-address=10.8.0.1 dst-address=$printerIp \
    protocol=tcp dst-port=631,8080 action=accept comment="vps-to-printer"
/ip firewall filter add chain=forward in-interface=wg-vps action=drop comment="drop other tunnel->lan"
/ip firewall filter add chain=input in-interface=wg-vps action=drop comment="no router access from tunnel"
# Do NOT masquerade traffic leaving through wg-vps (check /ip firewall nat).
