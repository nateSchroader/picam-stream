#!/usr/bin/env bash
# Install the Picamera2 MJPEG stream as a systemd service on a Raspberry Pi.
# Idempotent and safe to re-run. Run as a normal user; it uses sudo where needed.
#
#   ./install.sh                # app only (binds localhost -- see note at the end)
#   ./install.sh --with-proxy   # also install/configure a Caddy TLS + Basic Auth proxy
#
# Non-interactive proxy setup: set PICAM_PROXY_USER / PICAM_PROXY_PASS (and optionally
# PICAM_PROXY_HOST) in the environment to skip the prompts.
set -euo pipefail

WITH_PROXY=0
for arg in "$@"; do
  case "$arg" in
    --with-proxy) WITH_PROXY=1 ;;
    -h|--help) echo "Usage: $0 [--with-proxy]"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="$(id -un)"
UNIT=/etc/systemd/system/picam-stream.service

echo ">> picam-stream installer"
echo "   app dir : $APPDIR"
echo "   user    : $SERVICE_USER"

echo ">> Installing dependencies (python3-picamera2, python3-simplejpeg)..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends python3-picamera2 python3-simplejpeg

if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx video; then
  echo ">> Adding $SERVICE_USER to the 'video' group..."
  sudo usermod -aG video "$SERVICE_USER"
  echo "   (log out/in or reboot for the group change to fully apply)"
fi

echo ">> Installing systemd unit -> $UNIT"
sed -e "s|__USER__|${SERVICE_USER}|g" -e "s|__APPDIR__|${APPDIR}|g" \
    "$APPDIR/picam-stream.service" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable picam-stream.service
sudo systemctl restart picam-stream.service || \
  echo "   service did not start cleanly yet -- check the camera (below), then: journalctl -u picam-stream -e"

# ---- optional Caddy reverse proxy (TLS + Basic Auth) ----------------------------
if [ "$WITH_PROXY" -eq 1 ]; then
  echo ">> Setting up Caddy reverse proxy (TLS + Basic Auth)..."
  if ! command -v caddy >/dev/null 2>&1; then
    echo "   installing Caddy from the official repo..."
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y caddy
  fi

  PROXY_HOST="${PICAM_PROXY_HOST:-$(hostname).local}"
  PROXY_USER="${PICAM_PROXY_USER:-}"
  if [ -z "$PROXY_USER" ]; then
    read -r -p "   Basic-auth username [admin]: " PROXY_USER
    PROXY_USER="${PROXY_USER:-admin}"
  fi
  PROXY_PASS="${PICAM_PROXY_PASS:-}"
  if [ -z "$PROXY_PASS" ]; then
    read -r -s -p "   Basic-auth password: " PROXY_PASS; echo
  fi
  if [ -z "$PROXY_PASS" ]; then
    echo "   no password given; skipping proxy setup." >&2
    exit 1
  fi
  HASH="$(caddy hash-password --plaintext "$PROXY_PASS")"

  echo "   writing /etc/caddy/Caddyfile (host: $PROXY_HOST, user: $PROXY_USER)"
  sudo mkdir -p /etc/caddy
  sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${PROXY_HOST} {
    tls internal

    basic_auth {
        ${PROXY_USER} ${HASH}
    }

    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
EOF
  sudo systemctl enable caddy >/dev/null 2>&1 || true
  sudo systemctl restart caddy
  echo "   Caddy is serving https://${PROXY_HOST}/  (Basic Auth user: ${PROXY_USER})"
fi

echo ">> Checking for a camera..."
if rpicam-hello --list-cameras 2>/dev/null | grep -qE '^[0-9]+[[:space:]]*:'; then
  echo "   camera detected OK."
else
  cat <<'EOF'
   WARNING: no camera detected (rpicam-hello --list-cameras found none).
   For a Camera Module 2 on a Pi Zero: check the 22-pin ribbon cable, then set
       camera_auto_detect=0
       dtoverlay=imx219
   in /boot/firmware/config.txt and reboot. See README.md / CLAUDE.md.
EOF
fi

echo ">> Done."
if [ "$WITH_PROXY" -eq 1 ]; then
  echo "   View:    https://$(hostname).local/   (through Caddy: TLS + Basic Auth)"
  echo "   The Python app is bound to localhost; Caddy is the only public listener."
else
  echo "   The Python app is bound to LOCALHOST only (PICAM_BIND=127.0.0.1 in the unit)."
  echo "   For LAN access, re-run with --with-proxy, OR set PICAM_BIND=0.0.0.0 in"
  echo "   $UNIT (unauthenticated -- trusted networks only) and: sudo systemctl restart picam-stream"
fi
echo "   Manage:  systemctl status|restart|stop picam-stream    logs: journalctl -u picam-stream -f"
