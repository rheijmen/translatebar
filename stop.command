#!/bin/bash
# Stop everything TranslateBar started (bars, relay, tunnel). Double-click to clean up.
pkill -f "duo_web.py" 2>/dev/null
pkill -f "relay.py" 2>/dev/null
pkill -f "cloudflared tunnel" 2>/dev/null
pkill -f "host.py" 2>/dev/null
sleep 1
echo "Stopped all TranslateBar processes."
