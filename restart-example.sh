#!/bin/bash
# Example restart script for Assist
# Configure in Settings > Server Controls > Restart Command
# Default: assist restart
#
# Copy and customize this script for your setup, then set
# the "Restart Command" setting to its path.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# assist-ctl finds the PID file itself (per-user state dir, or ASSIST_PID_FILE
# in .env), so a restart needs no path here.
./assist-ctl restart
