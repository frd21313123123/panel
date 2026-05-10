#!/usr/bin/env bash
# Автоматическая установка и обновление Panel на Ubuntu 24.04.
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/panel}"
SERVICE_USER="${SERVICE_USER:-panel}"
APP_PORT="${APP_PORT:-8080}"
SITE_NAME="panel"
PANEL_DOMAIN="${PANEL_DOMAIN:-}"
PANEL_SSL_EMAIL="${PANEL_SSL_EMAIL:-}"
PANEL_MODE="${PANEL_MODE:-${1:-install}}"
PANEL_DOMAIN_WAS_SET=0
HTTPS_ENABLED=0
BACKUP_DIR=""

[ -n "${PANEL_DOMAIN}" ] && PANEL_DOMAIN_WAS_SET=1

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

usage() {
  echo "Использование:"
  echo "  sudo bash install.sh                 # установка"
  echo "  sudo bash install.sh update          # обновление существующей панели"
  echo ""
  echo "Переменные:"
  echo "  PANEL_DOMAIN=panel.example.com       # домен панели"
  echo "  PANEL_SSL_EMAIL=admin@example.com    # email для Let's Encrypt"
  echo "  INSTALL_DIR=/opt/panel               # путь установки"
}

normalize_mode() {
  case "${PANEL_MODE}" in
    install|--install|"")
      PANEL_MODE="install"
      ;;
    update|--update|upgrade|--upgrade)
      PANEL_MODE="update"
      ;;
    help|--help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[!] Неизвестный режим: ${PANEL_MODE}"
      usage
      exit 1
      ;;
  esac
}

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
  if [ "${PANEL_MODE}" = "update" ] && [ -z "${PANEL_DOMAIN}" ]; then
    PANEL_DOMAIN="$(read_existing_domain || true)"
  fi

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

read_existing_domain() {
  local config_path="/etc/nginx/sites-available/${SITE_NAME}"
  [ -f "${config_path}" ] || return 0

  awk '
    $1 == "server_name" {
      value=$2
      gsub(";", "", value)
      if (value != "_" && value != "") {
        print value
        exit
      }
    }
  ' "${config_path}"
}

read_existing_panel_secret() {
  local service_path="/etc/systemd/system/panel.service"
  [ -f "${service_path}" ] || return 0

  awk -F= '
    $1 == "Environment" && $2 == "PANEL_SECRET" {
      print substr($0, index($0, "PANEL_SECRET=") + length("PANEL_SECRET="))
      exit
    }
  ' "${service_path}"
}

existing_nginx_has_https() {
  local config_path="/etc/nginx/sites-available/${SITE_NAME}"
  [ -f "${config_path}" ] || return 1
  grep -Eq 'listen[[:space:]]+443|ssl_certificate' "${config_path}"
}

detect_existing_install() {
  [ -d "${INSTALL_DIR}/backend" ] || [ -f "/etc/systemd/system/panel.service" ]
}

maybe_switch_to_update() {
  [ "${PANEL_MODE}" = "install" ] || return 0
  detect_existing_install || return 0
  [ -t 0 ] || return 0

  local answer
  read -r -p "Обнаружена установленная панель в ${INSTALL_DIR}. Обновить её вместо установки? [Y/n]: " answer
  case "${answer}" in
    n|N|no|NO|No)
      PANEL_MODE="install"
      ;;
    *)
      PANEL_MODE="update"
      ;;
  esac
}

ask_ssl_email() {
  [ -z "${PANEL_DOMAIN}" ] && return 0

  if [ "${PANEL_MODE}" = "update" ] \
    && [ "${PANEL_DOMAIN_WAS_SET}" != "1" ] \
    && existing_nginx_has_https; then
    HTTPS_ENABLED=1
    return 0
  fi

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

backup_current_install() {
  detect_existing_install || {
    echo "[!] Панель в ${INSTALL_DIR} не найдена. Сначала выполните установку."
    exit 1
  }

  local timestamp
  timestamp="$(date +%Y%m%d-%H%M%S)"
  BACKUP_DIR="${INSTALL_DIR}/update-backups/${timestamp}"

  echo "[+] Создание резервной копии текущей панели..."
  mkdir -p "${BACKUP_DIR}"

  [ -d "${INSTALL_DIR}/backend" ] && cp -a "${INSTALL_DIR}/backend" "${BACKUP_DIR}/backend"
  [ -d "${INSTALL_DIR}/frontend" ] && cp -a "${INSTALL_DIR}/frontend" "${BACKUP_DIR}/frontend"
  [ -f "${INSTALL_DIR}/panel.db" ] && cp -a "${INSTALL_DIR}/panel.db" "${BACKUP_DIR}/panel.db"
  [ -f "/etc/systemd/system/panel.service" ] && cp -a "/etc/systemd/system/panel.service" "${BACKUP_DIR}/panel.service"
  [ -f "/etc/nginx/sites-available/${SITE_NAME}" ] && cp -a "/etc/nginx/sites-available/${SITE_NAME}" "${BACKUP_DIR}/nginx-${SITE_NAME}.conf"

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/update-backups" 2>/dev/null || true
}

update_app_files() {
  echo "[+] Обновление файлов панели в ${INSTALL_DIR}..."
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
  echo "[+] Настройка systemd-сервиса..."
  local panel_secret
  panel_secret="$(read_existing_panel_secret || true)"
  [ -n "${panel_secret}" ] || panel_secret="$(openssl rand -hex 32)"

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

  if [ "${PANEL_MODE}" = "update" ] \
    && [ "${PANEL_DOMAIN_WAS_SET}" != "1" ] \
    && [ -f "/etc/nginx/sites-available/${SITE_NAME}" ]; then
    echo "[+] Существующий nginx-конфиг сохранён."
    existing_nginx_has_https && HTTPS_ENABLED=1
    cat > /etc/nginx/conf.d/panel_ws_map.conf <<'MAPEOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
MAPEOF
    nginx -t
    systemctl reload nginx
    return 0
  fi

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
  if [ "${PANEL_MODE}" = "update" ]; then
    echo "[✓] Обновление завершено!"
  else
    echo "[✓] Установка завершена!"
  fi
  echo "    Панель: ${public_url}"
  if [ "${PANEL_MODE}" = "install" ]; then
    echo "    Логин по умолчанию: admin / admin"
    echo "    Сразу смените пароль после первого входа."
  fi
  [ -n "${BACKUP_DIR}" ] && echo "    Резервная копия: ${BACKUP_DIR}"
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

run_install() {
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
}

run_update() {
  ask_domain
  ask_ssl_email
  install_packages
  install_docker
  echo "[+] Проверка пользователя ${SERVICE_USER}..."
  id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd -r -s /bin/bash -m -d "${INSTALL_DIR}" "${SERVICE_USER}"
  usermod -aG docker "${SERVICE_USER}"
  backup_current_install
  update_app_files
  install_python_deps
  install_systemd_service
  write_nginx_config
  install_https
  print_result
}

normalize_mode
maybe_switch_to_update

if [ "${PANEL_MODE}" = "update" ]; then
  run_update
else
  run_install
fi
