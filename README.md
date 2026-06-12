# picam-stream

A lightweight, browser-viewable **MJPEG camera feed** for the **Raspberry Pi Camera
Module 2** (Sony IMX219), built with [Picamera2](https://github.com/raspberrypi/picamera2).
It runs as a systemd service — designed for a low-powered **Raspberry Pi Zero 2 W** — and
supports **live, persistent resolution switching** from the browser or a simple HTTP API.

## Features

- 📷 **Zero-install viewing** — open a URL in any browser on your LAN; no client app.
- 🎚️ **Dynamic resolution** — switch resolution live from a dropdown or via `curl`, with no
  restart. The choice **persists across reboots**.
- 🛡️ **Whitelisted resolutions** — a typo can't OOM a 512 MB board.
- ⚙️ **systemd service** — starts on boot, restarts on failure.
- 🪶 **Lightweight** — software-encoded JPEG, modest defaults tuned for the Zero 2 W.

## Requirements

- A Raspberry Pi with a CSI camera port, running **Raspberry Pi OS Bookworm or Trixie**
  (the `libcamera` / `rpicam` stack).
- A **Camera Module 2** (IMX219), or another libcamera-supported sensor (see *Other cameras*).
- `python3-picamera2` and `python3-simplejpeg` — installed for you by `install.sh`.

## Quick start

```bash
git clone <your-repo-url> picam-stream
cd picam-stream
./install.sh
```

Then open **`http://<your-pi>.local:8000/`** from any device on your network.

`install.sh` is idempotent: it installs the dependencies, installs and enables the systemd
service for the current user, and checks that a camera is detected.

## Camera not detected? (read this for Camera Module 2)

If `rpicam-hello --list-cameras` reports **`No cameras available!`**, it is almost always
physical, not software:

1. **Cabling first.** On a **Pi Zero**, the camera port is the narrow **22-pin** connector —
   the wide ribbon in the Module 2 box does **not** fit it; you need the Pi Zero camera
   adapter cable. Check the gold contacts face the right way and both latches are closed.
   CSI is **not** hot-pluggable — power-cycle after any cable change.
2. **Force the overlay.** If auto-detect (`camera_auto_detect=1`) still doesn't find it, set
   the following in `/boot/firmware/config.txt` and reboot:
   ```
   camera_auto_detect=0
   dtoverlay=imx219
   ```
   (Use the matching overlay for other sensors, e.g. `imx708` for Camera Module 3.)

The full debugging story is in [CLAUDE.md](CLAUDE.md).

## Usage

| What | How |
|---|---|
| **Web UI** | `http://<pi>:8000/` — live view + resolution dropdown |
| **Raw MJPEG** | `http://<pi>:8000/stream.mjpg` — embed in `<img>`, VLC, Home Assistant, … |
| **Current settings** | `curl http://<pi>:8000/config` → JSON |
| **Change resolution** | `curl -X POST "http://<pi>:8000/set_resolution?res=1280x720"` (optional `&fps=15`) |

Default resolution menu: `640x480, 800x600, 1024x768, 1280x720, 1640x1232, 1920x1080`.

## Configuration

Edit the constants at the top of [`mjpeg_server.py`](mjpeg_server.py):

- `PORT` — HTTP port (default `8000`)
- `ALLOWED` — the whitelist of selectable `(width, height)` resolutions
- `DEFAULT_SIZE`, `DEFAULT_FPS` — startup defaults
- `MIN_FPS` / `MAX_FPS` — clamp for the optional `fps` parameter

After editing, apply with `sudo systemctl restart picam-stream`.

The currently selected resolution is stored in `stream-config.json` (gitignored — it's
per-machine runtime state) and reloaded on startup, which is how the choice survives reboots.

## Managing the service

```bash
systemctl status picam-stream        # state
journalctl -u picam-stream -f        # live logs
sudo systemctl restart picam-stream  # restart
sudo systemctl stop picam-stream     # stop the feed
sudo systemctl disable picam-stream  # don't start on boot
```

## Performance notes

JPEG is encoded in **software** on the Pi Zero 2 W, so CPU — not the sensor — is the
bottleneck. 640×480 is smooth; higher resolutions deliver a lower effective frame rate.
Start modest and increase to taste.

## Security

⚠️ The stream is **unauthenticated** and bound to **all interfaces** on port 8000 — anyone on
your LAN can view it. That's fine for a trusted home network, but **do not port-forward it to
the internet as-is**. For remote access, put it behind a VPN (Tailscale / WireGuard) or an
authenticated reverse proxy.

## Other cameras

Any libcamera-supported sensor should work once it's detected
(`rpicam-hello --list-cameras`). You may want to tune `ALLOWED` to match the sensor's sizes.

## Files

```
picam-stream/
├── mjpeg_server.py       # the streaming server (Picamera2 + stdlib HTTP)
├── picam-stream.service  # systemd unit template (install.sh fills in user/path)
├── install.sh            # idempotent installer
├── README.md             # this file
├── CLAUDE.md             # context/onboarding for AI agents (the "why" + history)
└── stream-config.json    # runtime state (gitignored): current resolution
```
