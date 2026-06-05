#!/bin/bash
set -e

# Verify no global OpenCode configs leaked in
if [ -e /root/.config/opencode ]; then
  echo "FAIL: global OpenCode config detected — isolation breach"
  exit 1
fi

# Apply network whitelist (if NETWORK_WHITELIST=1)
if [ "$NETWORK_WHITELIST" = "1" ]; then
  /apply-network-whitelist.sh
fi

exec "$@"
