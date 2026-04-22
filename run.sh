#!/usr/bin/env bash
# Запуск Panel на Linux (Ubuntu 24)
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "[+] Создание venv..."
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "[+] Установка зависимостей..."
pip install -q -r backend/requirements.txt

# Проверка членства в группе docker
if ! groups | grep -q docker; then
  echo "[!] Ваш пользователь не в группе docker. Выполните:"
  echo "    sudo usermod -aG docker \$USER && newgrp docker"
fi

echo "[+] Запуск Panel на http://0.0.0.0:8080"
cd backend
exec python main.py
