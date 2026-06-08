#!/bin/bash
# ============================================
# GenCall One-Shot Deploy Script
# Run: chmod +x deploy.sh && ./deploy.sh
# ============================================

set -e

echo ""
echo "  ██████╗ ███████╗███╗   ██╗ ██████╗ █████╗ ██╗     ██╗"
echo " ██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔══██╗██║     ██║"
echo " ██║  ███╗█████╗  ██╔██╗ ██║██║     ███████║██║     ██║"
echo " ██║   ██║██╔══╝  ██║╚██╗██║██║     ██╔══██║██║     ██║"
echo " ╚██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║███████╗███████╗"
echo "  ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝"
echo ""
echo "  Deploying GenCall v2.0..."
echo ""

# ─── Detect package manager ──────────────────────────────────────────
if command -v yum &>/dev/null; then
    PKG="yum"
elif command -v apt &>/dev/null; then
    PKG="apt"
elif command -v dnf &>/dev/null; then
    PKG="dnf"
else
    echo "ERROR: No supported package manager found (yum/apt/dnf)"
    exit 1
fi
echo "[1/7] Package manager: $PKG"

# ─── Install system dependencies ─────────────────────────────────────
echo "[2/7] Installing system dependencies..."
if [ "$PKG" = "apt" ]; then
    apt update -qq
    apt install -y -qq git python3 python3-pip sip-tester 2>/dev/null || \
    apt install -y -qq git python3 python3-pip 2>/dev/null
else
    $PKG install -y git python3 python3-pip 2>/dev/null || true
    # Try to install SIPp
    $PKG install -y sip-tester 2>/dev/null || true
fi

# ─── Check Python version ────────────────────────────────────────────
PYTHON=""
for p in python3.12 python3.11 python3.10 python3; do
    if command -v $p &>/dev/null; then
        ver=$($p --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo $ver | cut -d. -f1)
        minor=$(echo $ver | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON=$p
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ required. Found:"
    python3 --version 2>&1 || echo "  No python3 found"
    echo ""
    echo "Install Python 3.11:"
    echo "  CentOS/RHEL: yum install -y python3.11"
    echo "  Ubuntu:      apt install -y python3.11"
    exit 1
fi
echo "[3/7] Using $PYTHON ($($PYTHON --version 2>&1))"

# ─── Clone or update repo ────────────────────────────────────────────
echo "[4/7] Getting GenCall source..."
INSTALL_DIR="/opt/VanDorial"
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git pull origin main
else
    rm -rf "$INSTALL_DIR"
    git clone https://github.com/mrbuttshooter/VanDorial.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ─── Install Python packages ─────────────────────────────────────────
echo "[5/7] Installing Python packages..."
$PYTHON -m pip install --upgrade pip 2>/dev/null || true
$PYTHON -m pip install fastapi uvicorn sqlalchemy pydantic httpx dpkt 2>/dev/null || \
$PYTHON -m pip install fastapi uvicorn sqlalchemy pydantic httpx 2>/dev/null

# ─── Create directories ──────────────────────────────────────────────
echo "[6/7] Creating directories..."
mkdir -p /opt/gencall/logs
mkdir -p /opt/gencall/media
mkdir -p /opt/gencall/scenarios/custom
mkdir -p /opt/gencall/data

# ─── Create systemd service ──────────────────────────────────────────
echo "[7/7] Creating systemd service..."
cat > /etc/systemd/system/gencall.service << SERVICEEOF
[Unit]
Description=GenCall SIP Traffic Generator v2.0
After=network.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=$PYTHON -c "import uvicorn; from gencall.main import create_app; app, config = create_app(); uvicorn.run(app, host='0.0.0.0', port=8080)"
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

# ─── Start the service ───────────────────────────────────────────────
systemctl daemon-reload
systemctl enable gencall
systemctl stop gencall 2>/dev/null || true
systemctl start gencall

# ─── Wait for it to come up ──────────────────────────────────────────
sleep 3

# ─── Get the IP ──────────────────────────────────────────────────────
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="YOUR_SERVER_IP"
fi

# ─── Check if it's running ───────────────────────────────────────────
if systemctl is-active --quiet gencall; then
    STATUS="RUNNING"
else
    STATUS="FAILED - check: journalctl -u gencall -n 50"
fi

# ─── Check if SIPp is available ──────────────────────────────────────
SIPP_STATUS="NOT INSTALLED"
if command -v sipp &>/dev/null; then
    SIPP_STATUS="$(sipp -v 2>&1 | head -1)"
fi

echo ""
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║        GenCall v2.0 - Deploy Complete         ║"
echo "  ╠═══════════════════════════════════════════════╣"
echo "  ║                                               ║"
echo "  ║  Status:    $STATUS"
echo "  ║  Dashboard: http://$SERVER_IP:8080            "
echo "  ║  API:       http://$SERVER_IP:8080/api/health "
echo "  ║  API Docs:  http://$SERVER_IP:8080/docs       "
echo "  ║  SIPp:      $SIPP_STATUS"
echo "  ║                                               ║"
echo "  ║  Commands:                                    ║"
echo "  ║    systemctl status gencall                   ║"
echo "  ║    systemctl restart gencall                  ║"
echo "  ║    journalctl -u gencall -f                   ║"
echo "  ║                                               ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""
