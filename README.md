# picam-stream

A lightweight, browser-viewable **MJPEG camera feed** for the **Raspberry Pi Camera
Module 2** (Sony IMX219), built with [Picamera2](https://github.com/raspberrypi/picamera2).
It runs as a systemd service — designed for a low-powered **Raspberry Pi Zero 2 W** — and
supports **live, persistent resolution switching** from the browser or a simple HTTP API.

## Features

- 📷 **Zero-install viewing** — open a URL in any browser; no client app.
- 🎚️ **Dynamic resolution** — switch resolution live from a dropdown or via `curl`, with no
  restart. The choice **persists across reboots**.
- 🎛️ **Camera controls** — flip/mirror, brightness, contrast, saturation, sharpness, and
  exposure/white-balance, live from the UI or API; persisted too.
- 📸 **Snapshots** — `/snapshot.jpg` returns the current frame (near-zero cost).
- ❤️ **Health endpoint** — `/healthz` reports CPU temp, real FPS, uptime, and client count.
- 🔒 **Secure by default** — binds to localhost and sits behind a TLS + Basic-Auth Caddy
  proxy (one flag: `./install.sh --with-proxy`).
- 🛡️ **Whitelisted resolutions** + a concurrent-stream cap — a typo (or a flood of clients)
  can't OOM a 512 MB board.
- ⚙️ **systemd service** — starts on boot, restarts on failure, sandboxed.
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
./install.sh --with-proxy     # TLS + Basic Auth via Caddy (recommended)
```

Then open **`https://<your-pi>.local/`** and log in with the username/password you chose.

`install.sh` is idempotent: it installs the dependencies, installs and enables the systemd
service for the current user, and checks that a camera is detected. With `--with-proxy` it
also installs Caddy and writes `/etc/caddy/Caddyfile` (prompting for a password, or reading
`PICAM_PROXY_USER`/`PICAM_PROXY_PASS` from the environment).

> **Plain `./install.sh` (no proxy)** binds the app to **localhost only**, so it isn't
> reachable from other devices until you add a proxy or opt into direct LAN access — see
> [Security](#security).

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

URLs below assume the Caddy proxy (`https://<pi>/…`). Behind it, the app's routes are
unchanged; if you run without the proxy they're at `http://<pi>:8000/…` (localhost-only by
default). For `curl` through the proxy, add `-u user:pass` and `-k` (Caddy's internal CA).

| What | Route | Notes |
|---|---|---|
| **Web UI** | `/` | live view, resolution dropdown, **camera-controls panel**, snapshot link |
| **Raw MJPEG** | `/stream.mjpg` | embed in `<img>`, VLC, Home Assistant, … |
| **Snapshot** | `/snapshot.jpg` | single still (the current frame) |
| **Current settings** | `/config` | JSON: resolution, fps, control values |
| **Health/metrics** | `/healthz` | JSON: CPU temp, real fps, uptime, client count, free RAM |
| **Change resolution** | `POST /set_resolution?res=1280x720` | optional `&fps=15` |
| **Change controls** | `POST /set_controls?brightness=0.1&hflip=1&awb=auto` | see below |

`set_resolution` and `set_controls` are **POST-only** (a `GET` returns 405).

```bash
# examples (through the proxy)
curl -k -u admin:secret "https://pi.local/snapshot.jpg" -o frame.jpg
curl -k -u admin:secret -X POST "https://pi.local/set_resolution?res=1280x720&fps=15"
curl -k -u admin:secret -X POST "https://pi.local/set_controls?contrast=1.4&hflip=1"
```

**Controls** (`/set_controls`, all optional): `brightness` (−1…1), `contrast`/`saturation`
(0…32), `sharpness` (0…16), `hflip`/`vflip`/`rotate=180`, `ae` (auto-exposure on/off),
`exposure` (µs) and `gain` when `ae=0`, `awb` (auto white-balance on/off), and `awbmode`
(`auto, incandescent, tungsten, fluorescent, indoor, daylight, cloudy`). Values are clamped;
the choices persist across restarts. Pass **`reset=1`** (or click **Reset to defaults** in the
controls panel) to revert every control and flip to its default.

Default resolution menu: `640x480, 800x600, 1024x768, 1280x720, 1640x1232, 1920x1080`.

## Configuration

Environment variables (set in the systemd unit, `Environment=…`):

- `PICAM_BIND` — bind address (default `127.0.0.1`; set `0.0.0.0` for direct LAN access)
- `PICAM_PORT` — HTTP port (default `8000`)
- `PICAM_MAX_STREAMS` — max concurrent MJPEG clients before `503` (default `4`)

Constants at the top of [`mjpeg_server.py`](mjpeg_server.py):

- `ALLOWED` — the whitelist of selectable `(width, height)` resolutions
- `DEFAULT_SIZE`, `DEFAULT_FPS` — startup defaults
- `MIN_FPS` / `MAX_FPS` — clamp for the optional `fps` parameter
- `LIVE_CONTROLS`, `EXPOSURE_RANGE`, `GAIN_RANGE`, `AWB_MODES` — control names + clamp ranges

After editing, apply with `sudo systemctl restart picam-stream`.

Selected resolution **and controls** are stored in `stream-config.json` and reloaded on
startup (how the choices survive reboots). Under systemd this lives in
**`/var/lib/picam-stream/`** (`StateDirectory`); for a manual `python3 mjpeg_server.py` run it
falls back to the script's own directory. It's gitignored, per-machine runtime state —
deleting it just resets to defaults.

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

The intended topology is **browser → Caddy (TLS + Basic Auth) → `127.0.0.1:8000` (app)**:

- The Python app **binds to localhost** (`PICAM_BIND=127.0.0.1`), so it is not directly
  reachable from the network — **Caddy is the only public listener**. It terminates HTTPS
  and enforces HTTP Basic Auth (see [`Caddyfile.example`](Caddyfile.example)).
- The **systemd unit is sandboxed** (`NoNewPrivileges`, `ProtectSystem=strict`, dropped
  capabilities, syscall filtering, …). Device access is left open on purpose — the camera
  needs it.
- A **concurrent-stream cap** (`PICAM_MAX_STREAMS`, default 4) returns `503` beyond the limit,
  so a client flood can't exhaust the 512 MB board. State-changing routes are **POST-only**.

`tls internal` uses Caddy's **local CA** (great for a LAN; trust its root on your devices to
silence browser warnings). With a publicly-resolvable domain, drop that line for automatic
Let's Encrypt certificates. Prefer **nginx**? Proxy the same way, but you must add
`proxy_buffering off;` on the stream location or the MJPEG feed will stall.

> **Direct LAN access without a proxy** (unauthenticated, plaintext): set `PICAM_BIND=0.0.0.0`
> in the unit and restart. Only do this on a trusted network, and **never port-forward it to
> the internet as-is** — use the proxy, a VPN (Tailscale / WireGuard), or both.

## Other cameras

Any libcamera-supported sensor should work once it's detected
(`rpicam-hello --list-cameras`). You may want to tune `ALLOWED` to match the sensor's sizes.

## Files

```
picam-stream/
├── mjpeg_server.py       # the streaming server (Picamera2 + stdlib HTTP)
├── picam-stream.service  # sandboxed systemd unit template (install.sh fills in user/path)
├── Caddyfile.example     # reverse-proxy template (TLS + Basic Auth) for manual setup
├── install.sh            # idempotent installer ( --with-proxy installs/configures Caddy )
├── README.md             # this file
└── CLAUDE.md             # context/onboarding for AI agents (the "why" + history)
```

Runtime state lives outside the repo at `/var/lib/picam-stream/stream-config.json` (current
resolution + controls); deleting it resets to defaults.
