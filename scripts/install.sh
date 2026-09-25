#!/usr/bin/env bash
# Install atak-bridge as a systemd service on Raspberry Pi OS (Bookworm, 64-bit).
# Run from the repository root:  sudo ./scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/atak-bridge
CONF_DIR=/etc/atak-bridge

apt-get update
apt-get install -y python3 python3-venv python3-pip

id -u atak >/dev/null 2>&1 || useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin atak
usermod -aG dialout atak

mkdir -p "$PREFIX" "$CONF_DIR"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$REPO_DIR"

if [[ ! -f "$CONF_DIR/config.toml" ]]; then
    install -m 0640 -o root -g atak "$REPO_DIR/config.example.toml" "$CONF_DIR/config.toml"
    echo "Created $CONF_DIR/config.toml - edit it before flying."
fi

install -m 0644 "$REPO_DIR/systemd/atak-bridge.service" /etc/systemd/system/atak-bridge.service
systemctl daemon-reload
systemctl enable --now atak-bridge

echo
echo "Installed. Logs:  journalctl -u atak-bridge -f"
echo "Point ATAK at this Pi: TCP port 8087 on $(hostname -I | awk '{print $1}')"
