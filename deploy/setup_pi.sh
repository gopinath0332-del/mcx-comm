#!/usr/bin/env bash
# deploy/setup_pi.sh
# Setup script for Raspberry Pi production server
# Run once after cloning the repo.
set -euo pipefail

echo "=== MCX Gold Engine — Raspberry Pi Setup ==="

# 1. Install system dependencies
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git

# 2. Create virtual environment
cd /home/pi/mcx-comm
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create data and logs directories
mkdir -p data logs

# 5. Copy .env template if .env doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  Edit .env and fill in your credentials:"
    echo "   - KITE_API_KEY"
    echo "   - DISCORD_WEBHOOK_URL"
    echo "   - NSE_ENV_PATH (path to nifty-strike-engine .env)"
fi

# 6. Install systemd service
sudo cp deploy/mcx-comm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mcx-comm.service

echo ""
echo "=== Setup complete ==="
echo "Start with: sudo systemctl start mcx-comm"
echo "Status:     sudo systemctl status mcx-comm"
echo "Logs:       sudo journalctl -u mcx-comm -f"
