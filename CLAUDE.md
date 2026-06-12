# CLAUDE.md — agent context for picam-stream

Operational context for an AI coding agent (e.g. Claude Code) working on this project.
Read this first; it captures the *why* and the non-obvious gotchas that aren't visible
from the code alone.

## What this is

A single-file Python MJPEG camera-streaming server (`mjpeg_server.py`) for a Raspberry Pi
Camera Module 2 (IMX219), run as a systemd service named `picam-stream`. It serves a browser
UI plus a raw MJPEG stream and supports **live, persistent resolution switching** over HTTP.

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

## Code map (`mjpeg_server.py`)

- `StreamingOutput` — thread-safe holder of the latest JPEG frame. `close()` is a deliberate
  **no-op** so the object survives the camera being stopped/restarted during a resolution
  change (otherwise the encoder's `FileOutput` could close it and break later writes).
- `CameraManager` — owns the `Picamera2` instance. `set_resolution()` serializes
  reconfiguration with a lock and saves state; `_load()/_save()` use `stream-config.json`.
- `StreamingHandler` — HTTP routes: `/` & `/index.html` (UI), `/stream.mjpg` (multipart
  MJPEG), `/config` (JSON state), `/set_resolution` (GET or POST, `res=WxH[&fps=N]`).
- Tunables at the top: `PORT`, `ALLOWED`, `DEFAULT_SIZE`, `DEFAULT_FPS`, `MIN/MAX_FPS`.

## Operational facts & gotchas

- **Service:** `picam-stream.service` (enabled on boot). Runs as the login user, who **must be
  in the `video` group** for camera access. Manage via `systemctl` / `journalctl -u picam-stream`.
- **Editing the server** requires `sudo systemctl restart picam-stream` to take effect.
- **`stream-config.json`** is runtime state (gitignored). Don't commit it; deleting it just
  resets to `DEFAULT_SIZE`.
- **Camera enablement** lives in `/boot/firmware/config.txt` (`dtoverlay=imx219`). Changing the
  camera or its cabling needs a **power-cycle/reboot** — CSI is not hot-pluggable.
- **Reboots drop the agent's session** if it's running on the Pi over SSH. Expect to reconnect
  and resume after any reboot.
- **Constraints:** 512 MB RAM; **software** JPEG encoding (CPU-bound). Keep defaults modest —
  high resolutions lower the effective frame rate, not the sensor's.
- **`vcgencmd get_camera` is unreliable** on the libcamera stack (often shows `detected=0` even
  when the camera works). Trust **`rpicam-hello --list-cameras`** instead.

## Quick recipes

- Camera healthy?  `rpicam-hello --list-cameras`  (expect an `imx219` entry)
- Feed up?  `systemctl is-active picam-stream` and
  `curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:8000/index.html`
- Change resolution:  `curl -X POST "http://localhost:8000/set_resolution?res=1280x720"`
- Fresh install on another Pi:  `./install.sh`

## Security

Unauthenticated, all-interfaces, port 8000 — LAN-only by design. Never expose it directly to
the internet; use a VPN or an authenticated reverse proxy.
