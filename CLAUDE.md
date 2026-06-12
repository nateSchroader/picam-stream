# CLAUDE.md — agent context for picam-stream

Operational context for an AI coding agent (e.g. Claude Code) working on this project.
Read this first; it captures the *why* and the non-obvious gotchas that aren't visible
from the code alone.

## What this is

A single-file Python MJPEG camera-streaming server (`mjpeg_server.py`) for a Raspberry Pi
Camera Module 2 (IMX219), run as a systemd service named `picam-stream`. It serves a browser
UI plus a raw MJPEG stream and supports **live, persistent resolution switching and camera
controls** (flip, brightness/contrast/etc., exposure, white balance) over HTTP, plus a
`/snapshot.jpg` still and a `/healthz` metrics endpoint. By default it **binds to localhost**
and is meant to sit behind a **Caddy reverse proxy** that terminates TLS + Basic Auth.

**Reference deployment:** a headless Raspberry Pi **Zero 2 W** (512 MB RAM, quad Cortex-A53)
running Raspberry Pi OS **Trixie**, accessed over SSH. The project lives in `~/picam-stream`
and the service runs as the login user (who must be in the `video` group).

## How we got here (the short version)

1. **Goal:** a simple, always-on camera feed for the Camera Module 2, viewable in a browser.
2. **Design choice:** MJPEG via **Picamera2** + a stdlib `http.server`, run under **systemd**.
   Chosen over RTSP/WebRTC (MediaMTX) and one-off `rpicam-vid` because it needs no client app
   and is the lightest "just works in a browser" option for a 512 MB board.
3. **The camera wasn't detected** (`No cameras available!`). Debugging showed it was **not** a
   software/config issue: `camera_auto_detect=1` was set correctly, but no sensor was visible
   on the CSI bus — no `imx219`/`unicam` lines in the kernel log, no `/dev/i2c-*`,
   `libcamera interfaces=0`. Two reboots changed nothing.
4. **Fix (physical + overlay):** the root cause was the CSI cable. On a Pi Zero you need the
   narrow **22-pin** adapter cable (the wide one in the Module 2 box doesn't fit), correctly
   oriented, latches closed — and CSI is **not** hot-pluggable. After reseating the cable
   **and** forcing the overlay (`camera_auto_detect=0` + `dtoverlay=imx219` in
   `/boot/firmware/config.txt`, then reboot), the IMX219 enumerated and a test capture worked.
   **We deliberately kept the explicit overlay** (deterministic + confirmed working) — do
   **not** revert it to auto-detect without re-testing on real hardware.
5. **Built the feed:** installed `python3-picamera2` + `python3-simplejpeg` (headless,
   `--no-install-recommends`), wrote `mjpeg_server.py`, created + enabled the systemd unit,
   verified it serves frames.
6. **Added dynamic resolution:** instead of editing code + restarting, the server reconfigures
   the **live** camera (stop → reconfigure → start) via `/set_resolution`, offers a dropdown
   UI, **whitelists** sizes (`ALLOWED`) for safety on the small board, and **persists** the
   choice to `stream-config.json` (reloaded on startup → survives reboots).
7. **Security-first expansion** (the current architecture): the app now **binds localhost**
   (`PICAM_BIND`, default `127.0.0.1`) and sits behind a **Caddy** reverse proxy doing TLS +
   Basic Auth (`Caddyfile.example`, `install.sh --with-proxy`). The systemd unit gained
   **sandboxing** (see gotcha below), a **concurrent-stream cap** (`PICAM_MAX_STREAMS` → 503),
   and **POST-only** mutation. Then features: `/snapshot.jpg` (returns the already-buffered
   frame — near-zero cost), **camera controls** (`/set_controls`, persisted alongside
   resolution), and `/healthz`. Config moved to `StateDirectory` (`/var/lib/picam-stream/`).

## Code map (`mjpeg_server.py`)

- `StreamingOutput` — thread-safe holder of the latest JPEG frame. `close()` is a deliberate
  **no-op** so the object survives the camera being stopped/restarted during a resolution
  change (otherwise the encoder's `FileOutput` could close it and break later writes). Also
  tracks recent frame timestamps → `fps()` for `/healthz`.
- `CameraManager` — owns the `Picamera2` instance. `set_resolution()` and `update_controls()`
  serialize (re)configuration with a lock and save state. **Flip changes go through the
  stop→reconfigure→start path** (`Transform` is set at configure time); other controls apply
  **live** via `set_controls()`. `_start()` reapplies persisted controls after every
  (re)start. `_load()/_save()` use `stream-config.json`; `validate_controls()` clamps/whitelists.
- `StreamingHandler` — routes: `/` & `/index.html` (UI), `/stream.mjpg` (multipart MJPEG,
  guarded by a `BoundedSemaphore` cap → 503), `/snapshot.jpg` (latest buffered frame),
  `/config` & `/healthz` (JSON), and **POST-only** `/set_resolution` (`res=WxH[&fps=N]`) and
  `/set_controls` (`brightness=…&hflip=1&awb=auto&awbmode=…`). GET on the setters → 405.
- Tunables/env at the top: `PORT`/`PICAM_PORT`, `BIND`/`PICAM_BIND`, `MAX_STREAMS`/
  `PICAM_MAX_STREAMS`, `ALLOWED`, `DEFAULT_SIZE`, `DEFAULT_FPS`, `MIN/MAX_FPS`, `LIVE_CONTROLS`,
  `EXPOSURE_RANGE`, `GAIN_RANGE`, `AWB_MODES`, `STATE_DIR`/`CONFIG_PATH`.

## Operational facts & gotchas

- **Service:** `picam-stream.service` (enabled on boot). Runs as the login user, who **must be
  in the `video` group** for camera access. Manage via `systemctl` / `journalctl -u picam-stream`.
- **Editing the server** requires `sudo systemctl restart picam-stream` to take effect.
- **Localhost bind by default** (`Environment=PICAM_BIND=127.0.0.1` in the unit): the app is
  **not** reachable from the LAN directly — Caddy is. To test on the Pi, curl `127.0.0.1:8000`.
  For direct LAN access set `PICAM_BIND=0.0.0.0` and restart (unauthenticated — trusted nets).
- **⚠️ systemd sandboxing vs. the camera:** the unit is hardened (`ProtectSystem=strict`,
  `NoNewPrivileges`, syscall filter, dropped caps…). The camera needs a broad device surface
  (`/dev/video*`, `/dev/media*`, `/dev/dma_heap*`), so **do NOT add `PrivateDevices` or
  `DevicePolicy=closed`** — that breaks capture. If the service fails to start after a
  hardening change, `journalctl -u picam-stream -e` and relax the offending directive (usually
  `ProtectSystem` or `SystemCallFilter`). `MemoryDenyWriteExecute` is intentionally **off**.
- **Caddy proxy:** config at `/etc/caddy/Caddyfile` (generated by `install.sh --with-proxy`;
  template `Caddyfile.example`). **Gotcha:** the `reverse_proxy` block needs `flush_interval -1`
  or the MJPEG stream stalls (proxy buffers the endless multipart response). Manage with
  `systemctl … caddy` / `journalctl -u caddy`. `tls internal` ⇒ Caddy's local CA.
- **`stream-config.json`** is runtime state (gitignored), now under **`/var/lib/picam-stream/`**
  (`StateDirectory`) when run by systemd. Holds resolution **+ controls**; deleting it resets
  to defaults.
- **Camera enablement** lives in `/boot/firmware/config.txt` (`dtoverlay=imx219`). Changing the
  camera or its cabling needs a **power-cycle/reboot** — CSI is not hot-pluggable.
- **Reboots drop the agent's session** if it's running on the Pi over SSH. Expect to reconnect
  and resume after any reboot.
- **Constraints:** 512 MB RAM; **software** JPEG encoding (CPU-bound). Keep defaults modest —
  high resolutions lower the effective frame rate, not the sensor's. The stream cap
  (`PICAM_MAX_STREAMS`, default 4) bounds concurrent clients.
- **`vcgencmd get_camera` is unreliable** on the libcamera stack (often shows `detected=0` even
  when the camera works). Trust **`rpicam-hello --list-cameras`** instead. (`vcgencmd
  get_throttled` *is* reliable — handy for spotting undervoltage.)

## Quick recipes

- Camera healthy?  `rpicam-hello --list-cameras`  (expect an `imx219` entry)
- Feed up? (on the Pi — app is localhost-bound)  `systemctl is-active picam-stream` and
  `curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/index.html`
- Health:  `curl -s http://127.0.0.1:8000/healthz`  (temp, fps, uptime, clients, free RAM)
- Change resolution:  `curl -X POST "http://127.0.0.1:8000/set_resolution?res=1280x720"`
- Flip / control:  `curl -X POST "http://127.0.0.1:8000/set_controls?hflip=1&contrast=1.4"`
- Through the proxy:  `curl -k -u USER:PASS https://$(hostname).local/healthz`
- Check hardening:  `systemd-analyze security picam-stream`
- Fresh install on another Pi:  `./install.sh --with-proxy`

## Security

Topology: **browser → Caddy (TLS + Basic Auth) → `127.0.0.1:8000` (app)**. The app binds
localhost, so Caddy is the only public listener; the systemd unit is sandboxed; a
concurrent-stream cap and POST-only setters limit abuse. `PICAM_BIND=0.0.0.0` re-exposes it
unauthenticated for a trusted LAN. Never port-forward it to the internet without the proxy
(and/or a VPN). See README → Security and `Caddyfile.example`.
