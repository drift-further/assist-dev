#!/bin/bash
# Example restart script for Assist
# Configure in Settings > Server Controls > Restart Command
# Default: assist restart
#
# Copy and customize this script for your setup, then set
# the "Restart Command" setting to its path.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kill existing process
if [ -f /tmp/assist-server.pid ]; then
    kill "$(cat /tmp/assist-server.pid)" 2>/dev/null
    sleep 1
fi

# Start fresh
./assist-ctl start
