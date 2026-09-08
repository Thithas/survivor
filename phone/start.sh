#!/data/data/com.termux/files/usr/bin/bash
# Keeps the relay alive. Run inside Termux:  bash ~/survivor/start.sh
cd ~/survivor
set -a; source .env; set +a
termux-wake-lock 2>/dev/null || true
while true; do
  python relay.py
  echo "relay stopped, restarting in 5s"; sleep 5
done
