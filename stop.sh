#!/bin/bash
# ==============================================================================
# stop.sh
# Stop script for MCX Gold Commodity Trading Engine on Raspberry Pi / Linux
# ==============================================================================

SERVICE_NAME="mcx-comm"

echo "=========================================================="
echo "🛑 Stopping MCX Commodity Engine ($SERVICE_NAME)..."
echo "=========================================================="

# 1. Stop systemd service
if systemctl list-unit-files | grep -q "^$SERVICE_NAME.service"; then
    echo "Stopping $SERVICE_NAME..."
    sudo systemctl stop $SERVICE_NAME
    echo "✅ $SERVICE_NAME stopped."
else
    echo "WARNING: $SERVICE_NAME.service not found in systemd."
fi

# 2. Check current status
sleep 1
echo ""
echo "Current Service Status:"
sudo systemctl status $SERVICE_NAME --no-pager || true

echo "=========================================================="
echo "To restart: ./restart.sh or sudo systemctl start $SERVICE_NAME"
echo "=========================================================="
