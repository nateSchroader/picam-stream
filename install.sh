#!/usr/bin/env bash
# Install the Picamera2 MJPEG stream as a systemd service on a Raspberry Pi.
# Idempotent and safe to re-run. Run as a normal user; it uses sudo where needed.
set -euo pipefail

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

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ">> Done."
echo "   Stream:  http://$(hostname).local:8000/   or   http://${IP}:8000/"
echo "   Manage:  systemctl status|restart|stop picam-stream    logs: journalctl -u picam-stream -f"
