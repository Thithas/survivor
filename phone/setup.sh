#!/data/data/com.termux/files/usr/bin/bash
# One-time Termux setup for the SURVIVOR phone relay. Run:  bash setup.sh
set -e
pkg update -y && pkg install -y python cloudflared git termux-api
pip install --upgrade pip requests
termux-wake-lock || true
mkdir -p ~/survivor && cd ~/survivor
curl -sL https://raw.githubusercontent.com/Thithas/survivor/main/phone/relay.py -o relay.py
curl -sL https://raw.githubusercontent.com/Thithas/survivor/main/phone/start.sh -o start.sh
[ -f .env ] || printf 'GH_TOKEN=paste_your_github_token_here\nREPO=Thithas/survivor\n' > .env
echo
echo "Now edit ~/survivor/.env and put your GitHub token in it (nano ~/survivor/.env), then:  bash ~/survivor/start.sh"
