#!/usr/bin/env bash
# One-shot installer for Ubuntu 22.04/24.04. Run as root from the repository checkout.
set -euo pipefail
APP=/opt/dhan-quant
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl
id dhan >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin dhan
mkdir -p "$APP" "$APP/data" "$APP/logs" "$APP/deploy"
cp main.py index.html requirements.txt holidays.txt .env.example "$APP/"
cp deploy/* "$APP/deploy/"
[ -f "$APP/.env" ] || cp .env.example "$APP/.env"
chmod 600 "$APP/.env"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"
chown -R dhan:dhan "$APP"
sudo -u dhan "$APP/.venv/bin/python" "$APP/main.py" initdb
sudo -u dhan "$APP/.venv/bin/python" "$APP/main.py" selftest
timedatectl set-timezone Asia/Kolkata || true
cp "$APP/deploy/dhan-quant-engine.service" /etc/systemd/system/
cp "$APP/deploy/dhan-quant-healthcheck.service" "$APP/deploy/dhan-quant-healthcheck.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dhan-quant-engine.service
systemctl enable --now dhan-quant-healthcheck.timer
echo "Installed. Edit $APP/.env, then: systemctl restart dhan-quant-engine"
