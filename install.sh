#!/usr/bin/env bash
# Удобная точка входа для установки и обновления панели на Ubuntu.
set -e
cd "$(dirname "$0")"
exec bash ./install-ubuntu.sh "$@"
