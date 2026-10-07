#!/usr/bin/env bash
# Install, or update, Pawblic Address as a systemd service that runs as you, from this
# folder. Run it as the user who owns the app (not root, not with sudo); it asks for sudo
# only to install and start the service. Safe to run again: it keeps settings.json and
# the certificate, and restarts the service.
#
#   ./deploy/install-service.sh              web page on port 7100
#   PORT=8443 ./deploy/install-service.sh    another port
set -euo pipefail

SERVICE=pawblic-address
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PORT="${PORT:-7100}"
UNIT="/etc/systemd/system/$SERVICE.service"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as the user who will own the app, not as root or with sudo"
[[ "$APP_DIR" =~ ^[A-Za-z0-9_./-]+$ ]] || die "move the app to a folder whose path has no spaces or special characters (now: $APP_DIR)"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] \
  || die "PORT must be a number from 1024 to 65535 (the service doesn't run as root)"
command -v systemctl >/dev/null || die "systemd not found; see the README for running in a terminal"
command -v sudo >/dev/null || die "sudo not found; ask an admin to install the service file (see the README)"
command -v python3 >/dev/null || die "python3 not found: sudo apt install python3 python3-venv"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is needed"
command -v ffmpeg >/dev/null || die "ffmpeg not found: sudo apt install ffmpeg"
command -v openssl >/dev/null || die "openssl not found: sudo apt install openssl"

say "Python environment: $APP_DIR/.venv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv" || die "couldn't create the venv: sudo apt install python3-venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"

if [ -f "$APP_DIR/cert.pem" ] && [ -f "$APP_DIR/key.pem" ]; then
  say "HTTPS certificate: keeping the existing cert.pem/key.pem"
else
  say "HTTPS certificate: making a self-signed one (phones only allow the mic over HTTPS)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$SERVICE" \
    -keyout "$APP_DIR/key.pem" -out "$APP_DIR/cert.pem" 2>/dev/null
fi
chmod 600 "$APP_DIR/key.pem"

say "Service: $UNIT (running as $RUN_USER, port $PORT); sudo may ask for your password"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@PORT@|$PORT|g" \
  "$APP_DIR/deploy/$SERVICE.service" > "$tmp"
sudo install -m 644 "$tmp" "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

say "Checking that it answers"
for _ in $(seq 30); do
  if version="$("$APP_DIR/.venv/bin/python" - "$PORT" 2>/dev/null <<'PY'
import json, ssl, sys, urllib.request
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
with urllib.request.urlopen(f"https://127.0.0.1:{sys.argv[1]}/api/health", timeout=2, context=ctx) as r:
    print(json.load(r)["version"])
PY
)"; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '\nPawblic Address v%s is running and starts at boot.\n' "$version"
    printf '  On a phone, open:  https://%s:%s\n' "${ip:-<this-server-ip>}" "$PORT"
    printf '  Logs:              journalctl -u %s -f\n' "$SERVICE"
    if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
      printf '  Firewall is on:    sudo ufw allow %s/tcp\n' "$PORT"
    fi
    exit 0
  fi
  sleep 0.5
done
sudo systemctl --no-pager status "$SERVICE" || true
die "the service didn't answer on port $PORT. See: journalctl -u $SERVICE -e"
