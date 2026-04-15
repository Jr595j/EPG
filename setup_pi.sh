#!/bin/bash
# setup_pi.sh — Run this on your Raspberry Pi to install and set up EPG Aggregator
# Usage: bash setup_pi.sh

set -e

INSTALL_DIR="$HOME/epg-aggregator"
SERVICE_NAME="epg-aggregator"

echo "============================================================"
echo " EPG Aggregator — Raspberry Pi Setup"
echo "============================================================"

# 1. Copy files (assumes you've already SCP'd the project folder here)
echo "[1/4] Installing dependencies..."
pip3 install flask requests apscheduler --quiet

# 2. Create systemd service so it auto-starts on boot
echo "[2/4] Creating systemd service..."
PYTHON_PATH=$(which python3)
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=EPG Aggregator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_PATH $INSTALL_DIR/server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 3. Enable and start the service
echo "[3/4] Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# 4. Show status
echo "[4/4] Done!"
echo ""
PI_IP=$(hostname -I | awk '{print $1}')
echo "  Service status : sudo systemctl status $SERVICE_NAME"
echo "  Dashboard      : http://$PI_IP:8080/"
echo "  EPG URL        : http://$PI_IP:8080/epg.xml"
echo ""
echo "Point TiviMate to the EPG URL above."
echo "============================================================"
