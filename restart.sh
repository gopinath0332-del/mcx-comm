#!/bin/bash
# ==============================================================================
# restart.sh
# Restart script for MCX Gold Commodity Trading Engine on Raspberry Pi / Linux
# ==============================================================================

SERVICE_NAME="mcx-comm"

echo "=========================================================="
echo "🔄 Restarting MCX Commodity Engine ($SERVICE_NAME)..."
echo "=========================================================="

# 1. Restart systemd service
if systemctl list-unit-files | grep -q "^$SERVICE_NAME.service"; then
    echo "Restarting $SERVICE_NAME..."
    sudo systemctl restart $SERVICE_NAME
else
    echo "WARNING: $SERVICE_NAME.service not found in systemd."
    echo "If running manually, kill existing and run: python main.py"
fi

# 2. Wait for process initialization
sleep 2

# 3. Check service status
echo ""
echo "Service Status:"
sudo systemctl status $SERVICE_NAME --no-pager || true

# 4. Show recent journal logs
echo ""
echo "Recent Logs:"
sudo journalctl -u $SERVICE_NAME -n 15 --no-pager

echo "=========================================================="
echo "Follow live logs with: sudo journalctl -u $SERVICE_NAME -f"
echo "=========================================================="
