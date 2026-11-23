#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "[LanDesk] PERINGATAN: sesi Wayland. Logout lalu pilih sesi 'Xorg' di layar login."
fi

if [ ! -x .venv/bin/python ]; then
  echo "[LanDesk] Membuat virtualenv..."
  python3 -m venv .venv || { echo "Install dulu: sudo apt install python3-venv python3-pip"; exit 1; }
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python server.py "$@"
