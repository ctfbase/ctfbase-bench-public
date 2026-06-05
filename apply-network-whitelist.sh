#!/bin/bash
# Apply iptables whitelist from config/network-whitelist.txt
# Requires: --cap-add=NET_ADMIN or --privileged
set -e

WHITELIST_FILE="/workspace/config/network-whitelist.txt"
if [ ! -f "$WHITELIST_FILE" ]; then
  # Fallback: look in the mounted benchmark directory
  WHITELIST_FILE="/bench-config/network-whitelist.txt"
fi

if [ ! -f "$WHITELIST_FILE" ]; then
  echo "WARNING: network whitelist file not found, skipping iptables rules"
  exit 0
fi

echo "Applying network whitelist from $WHITELIST_FILE"

# Create ipset for allowed IPs
ipset create allowed_hosts hash:ip 2>/dev/null || true

# Resolve each domain and add to ipset
while IFS= read -r line; do
  # Skip comments and empty lines
  line=$(echo "$line" | sed 's/#.*//' | tr -d '[:space:]')
  [ -z "$line" ] && continue

  # Resolve domain to IPs
  ips=$(dig +short "$line" A 2>/dev/null | grep -E '^[0-9]+\.' || true)
  if [ -z "$ips" ]; then
    echo "  WARN: could not resolve $line"
    continue
  fi

  for ip in $ips; do
    ipset add allowed_hosts "$ip" 2>/dev/null || true
    echo "  $line → $ip"
  done
done < "$WHITELIST_FILE"

# Apply iptables rules
# Allow loopback
iptables -A OUTPUT -o lo -j ACCEPT

# Allow DNS (needed for resolution)
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# Allow established connections
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow traffic to docker bridge network (for compose tasks)
# Docker bridge is typically 172.16.0.0/12
iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT
iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT
iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT

# Allow whitelisted hosts
iptables -A OUTPUT -m set --match-set allowed_hosts dst -j ACCEPT

# Default deny everything else (outbound)
iptables -A OUTPUT -j DROP

echo "Network whitelist applied: $(ipset list allowed_hosts | grep -c 'Members:' || echo 0) rules"
