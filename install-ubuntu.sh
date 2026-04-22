#!/usr/bin/env bash
# Установка Panel на Ubuntu 24.04 (production-ready через systemd)
set -e

INSTALL_DIR="/opt/panel"
SERVICE_USER="panel"

if [ "$EUID" -ne 0 ]; then echo "Запустите от root: sudo ./install-ubuntu.sh"; exit 1; fi

echo "[+] Установка системных пакетов..."
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip curl ca-certificates nginx

if ! command -v docker >/dev/null 2>&1; then
  echo "[+] Установка Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

echo "[+] Создание пользователя ${SERVICE_USER}..."
id -u ${SERVICE_USER} >/dev/null 2>&1 || useradd -r -s /bin/bash -m -d ${INSTALL_DIR} ${SERVICE_USER}
usermod -aG docker ${SERVICE_USER}

echo "[+] Копирование файлов в ${INSTALL_DIR}..."
mkdir -p ${INSTALL_DIR}
cp -r backend frontend ${INSTALL_DIR}/
chown -R ${SERVICE_USER}:${SERVICE_USER} ${INSTALL_DIR}

echo "[+] Установка зависимостей Python..."
sudo -u ${SERVICE_USER} python3 -m venv ${INSTALL_DIR}/.venv
sudo -u ${SERVICE_USER} ${INSTALL_DIR}/.venv/bin/pip install --quiet --upgrade pip
sudo -u ${SERVICE_USER} ${INSTALL_DIR}/.venv/bin/pip install --quiet -r ${INSTALL_DIR}/backend/requirements.txt

echo "[+] Создание systemd-сервиса..."
PANEL_SECRET=$(openssl rand -hex 32)
cat > /etc/systemd/system/panel.service <<EOF
[Unit]
Description=Panel — code container management
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}/backend
Environment=PANEL_SECRET=${PANEL_SECRET}
Environment=PANEL_DATA_ROOT=${INSTALL_DIR}/data/servers
Environment=PANEL_DB=sqlite:///${INSTALL_DIR}/panel.db
ExecStart=${INSTALL_DIR}/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable panel
systemctl restart panel
sleep 2
systemctl status panel --no-pager || true

echo "[+] Настройка nginx..."
# map-директива должна быть на уровне http{}, поэтому в conf.d
cat > /etc/nginx/conf.d/panel_ws_map.conf <<'MAPEOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
MAPEOF

cp nginx.conf /etc/nginx/sites-available/panel
ln -sf /etc/nginx/sites-available/panel /etc/nginx/sites-enabled/panel
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx

echo ""
echo "[✓] Установка завершена!"
echo "    Panel запущен на http://$(curl -s ifconfig.me 2>/dev/null || echo '<server-ip>')"
echo "    Логин по умолчанию: admin / admin  ← СМЕНИТЕ ПАРОЛЬ!"
echo ""
echo "    Добавить SSL (Let's Encrypt):"
echo "      apt install certbot python3-certbot-nginx"
echo "      certbot --nginx -d your.domain.com"
echo ""
echo "    Логи: journalctl -u panel -f"
