#!/usr/bin/env bash
# Автоматическая установка Panel на Ubuntu 24.04: Docker, Python, systemd, nginx и HTTPS.
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/panel}"
SERVICE_USER="${SERVICE_USER:-panel}"
APP_PORT="${APP_PORT:-8080}"
SITE_NAME="panel"
PANEL_DOMAIN="${PANEL_DOMAIN:-}"
PANEL_SSL_EMAIL="${PANEL_SSL_EMAIL:-}"
HTTPS_ENABLED=0

if [ "$EUID" -ne 0 ]; then
  echo "Запустите от root: sudo ./install-ubuntu.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [ ! -d "backend" ] || [ ! -d "frontend" ] || [ ! -f "backend/requirements.txt" ]; then
  echo "[!] Запустите установщик из корня проекта Panel."
  exit 1
fi

case "${INSTALL_DIR}" in
  ""|"/"|"/opt"|"/usr"|"/home")
    echo "[!] Небезопасный INSTALL_DIR: ${INSTALL_DIR}"
    exit 1
    ;;
esac

normalize_domain() {
  local value="$1"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  value="${value%%:*}"
  value="$(echo "$value" | tr '[:upper:]' '[:lower:]' | xargs)"
  echo "$value"
}

is_valid_domain() {
  local value="$1"
  [[ "$value" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]
}

ask_domain() {
  if [ -n "${PANEL_DOMAIN}" ]; then
    PANEL_DOMAIN="$(normalize_domain "${PANEL_DOMAIN}")"
  elif [ -t 0 ]; then
    while true; do
      read -r -p "Введите домен для панели (например panel.example.com, Enter = доступ по IP): " PANEL_DOMAIN
      PANEL_DOMAIN="$(normalize_domain "${PANEL_DOMAIN}")"
      [ -z "${PANEL_DOMAIN}" ] && break
      is_valid_domain "${PANEL_DOMAIN}" && break
      echo "[!] Некорректный домен. Введите домен без http://, порта и пути."
    done
  fi

  if [ -n "${PANEL_DOMAIN}" ] && ! is_valid_domain "${PANEL_DOMAIN}"; then
    echo "[!] Некорректный домен в PANEL_DOMAIN: ${PANEL_DOMAIN}"
    exit 1
  fi
}

ask_ssl_email() {
  [ -z "${PANEL_DOMAIN}" ] && return 0

  if [ -n "${PANEL_SSL_EMAIL}" ]; then
    return 0
  fi

  if [ -t 0 ]; then
    read -r -p "Email для Let's Encrypt (Enter = HTTPS не настраивать автоматически): " PANEL_SSL_EMAIL
    PANEL_SSL_EMAIL="$(echo "${PANEL_SSL_EMAIL}" | xargs)"
  fi
}

install_packages() {
  echo "[+] Установка системных пакетов..."
  apt-get update -qq
  apt-get install -y python3 python3-venv python3-pip curl ca-certificates nginx openssl
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[+] Установка Docker..."
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker
}

install_app_files() {
  echo "[+] Создание пользователя ${SERVICE_USER}..."
  id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd -r -s /bin/bash -m -d "${INSTALL_DIR}" "${SERVICE_USER}"
  usermod -aG docker "${SERVICE_USER}"

  echo "[+] Копирование файлов в ${INSTALL_DIR}..."
  mkdir -p "${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}/backend" "${INSTALL_DIR}/frontend"
  cp -a backend frontend "${INSTALL_DIR}/"
  mkdir -p "${INSTALL_DIR}/data/servers" "${INSTALL_DIR}/data/backups"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
}

install_python_deps() {
  echo "[+] Установка зависимостей Python..."
  if [ ! -x "${INSTALL_DIR}/.venv/bin/python" ]; then
    sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
  fi
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
  sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/backend/requirements.txt"
}

install_systemd_service() {
  echo "[+] Создание systemd-сервиса..."
  local panel_secret
  panel_secret="$(openssl rand -hex 32)"

  cat > /etc/systemd/system/panel.service <<EOF
[Unit]
Description=Panel code container management
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}/backend
Environment=PANEL_SECRET=${panel_secret}
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
}

write_nginx_config() {
  echo "[+] Настройка nginx..."
  local server_name="_"
  [ -n "${PANEL_DOMAIN}" ] && server_name="${PANEL_DOMAIN}"

  cat > /etc/nginx/conf.d/panel_ws_map.conf <<'MAPEOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
MAPEOF

  cat > "/etc/nginx/sites-available/${SITE_NAME}" <<EOF
server {
    listen 80;
    server_name ${server_name};

    client_max_body_size 110M;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;

        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection \$connection_upgrade;

        proxy_read_timeout  3600s;
        proxy_send_timeout  3600s;
    }
}
EOF

  ln -sf "/etc/nginx/sites-available/${SITE_NAME}" "/etc/nginx/sites-enabled/${SITE_NAME}"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx
}

install_https() {
  [ -z "${PANEL_DOMAIN}" ] && return 0
  [ -z "${PANEL_SSL_EMAIL}" ] && return 0

  echo "[+] Настройка HTTPS для ${PANEL_DOMAIN}..."
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx \
    -d "${PANEL_DOMAIN}" \
    --non-interactive \
    --agree-tos \
    --email "${PANEL_SSL_EMAIL}" \
    --redirect && HTTPS_ENABLED=1 || {
      echo "[!] Не удалось выпустить HTTPS-сертификат. Проверьте, что DNS домена указывает на этот сервер."
      echo "[!] Панель оставлена доступной по HTTP."
    }
}

print_result() {
  local public_url
  if [ -n "${PANEL_DOMAIN}" ]; then
    public_url="http://${PANEL_DOMAIN}"
    [ "${HTTPS_ENABLED}" = "1" ] && public_url="https://${PANEL_DOMAIN}"
  else
    public_url="http://$(curl -s ifconfig.me 2>/dev/null || echo '<server-ip>')"
  fi

  echo ""
  echo "[✓] Установка завершена!"
  echo "    Панель: ${public_url}"
  echo "    Логин по умолчанию: admin / admin"
  echo "    Сразу смените пароль после первого входа."
  echo ""
  echo "    Логи: journalctl -u panel -f"
  echo "    Перезапуск: systemctl restart panel"

  if [ -n "${PANEL_DOMAIN}" ] && [ "${HTTPS_ENABLED}" != "1" ]; then
    echo ""
    echo "    HTTPS не был включён автоматически."
    echo "    Чтобы включить позже:"
    echo "      apt install certbot python3-certbot-nginx"
    echo "      certbot --nginx -d ${PANEL_DOMAIN}"
  fi
}

ask_domain
ask_ssl_email
install_packages
install_docker
install_app_files
install_python_deps
install_systemd_service
write_nginx_config
install_https
print_result
