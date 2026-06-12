#!/usr/bin/python3
"""
MJPEG camera stream for the Raspberry Pi Camera Module 2 (IMX219) on a Pi Zero 2 W,
using Picamera2 -- with live, dynamic resolution switching and camera controls.

  Browser UI:         http://<pi>:8000/            (resolution + controls in the header)
  Raw MJPEG stream:   http://<pi>:8000/stream.mjpg
  Single still:       http://<pi>:8000/snapshot.jpg
  Current config:     http://<pi>:8000/config      (JSON)
  Health/metrics:     http://<pi>:8000/healthz     (JSON)
  Change resolution:  POST http://<pi>:8000/set_resolution?res=1280x720   (optional &fps=15)
  Change controls:    POST http://<pi>:8000/set_controls?brightness=0.1&hflip=1&awb=auto

The selected resolution/controls are applied to the live camera (no service restart) and
saved to stream-config.json (under $STATE_DIRECTORY when run by systemd), so they persist
across restarts/reboots. Resolutions are whitelisted (see ALLOWED) to stay safe on the
512 MB board.

Security note: by default this binds to localhost only (PICAM_BIND=127.0.0.1) and is meant
to sit behind a TLS-terminating, authenticating reverse proxy (see Caddyfile.example). Set
PICAM_BIND=0.0.0.0 to serve directly on the LAN (unauthenticated -- trusted networks only).
"""

import io
import json
import logging
import os
import socket
import socketserver
import time
from collections import deque
from http import server
from threading import BoundedSemaphore, Condition, Lock
from urllib.parse import urlparse, parse_qs

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

# ---- Config ---------------------------------------------------------------
PORT = int(os.environ.get("PICAM_PORT", "8000"))
# Bind address. Defaults to localhost: the app expects a reverse proxy in front.
# Set PICAM_BIND=0.0.0.0 to expose directly on the LAN (unauthenticated).
BIND = os.environ.get("PICAM_BIND", "127.0.0.1")
# Max concurrent MJPEG stream clients (each holds a thread on a 512 MB board).
MAX_STREAMS = int(os.environ.get("PICAM_MAX_STREAMS", "4"))

# Whitelisted (width, height) options offered in the UI and accepted by the API.
ALLOWED = [
    (640, 480),
    (800, 600),
    (1024, 768),
    (1280, 720),
    (1640, 1232),
    (1920, 1080),
]
DEFAULT_SIZE = (640, 480)
DEFAULT_FPS = 20
MIN_FPS, MAX_FPS = 5, 30

# Runtime state dir: systemd sets STATE_DIRECTORY (e.g. /var/lib/picam-stream); fall back to
# the script's own directory for manual runs. Keeps a strict-FS sandbox from blocking writes.
STATE_DIR = os.environ.get("STATE_DIRECTORY", "").split(":")[0] \
    or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(STATE_DIR, "stream-config.json")

# Live camera controls: user-key -> (Picamera2 control name, min, max, default).
LIVE_CONTROLS = {
    "brightness": ("Brightness", -1.0, 1.0, 0.0),
    "contrast": ("Contrast", 0.0, 32.0, 1.0),
    "saturation": ("Saturation", 0.0, 32.0, 1.0),
    "sharpness": ("Sharpness", 0.0, 16.0, 1.0),
}
EXPOSURE_RANGE = (100, 1_000_000)   # microseconds, only meaningful with AE off
GAIN_RANGE = (1.0, 16.0)            # AnalogueGain
# AWB mode name -> libcamera AwbModeEnum value.
AWB_MODES = {
    "auto": 0, "incandescent": 1, "tungsten": 2, "fluorescent": 3,
    "indoor": 4, "daylight": 5, "cloudy": 6,
}
AWB_BY_VALUE = {v: k for k, v in AWB_MODES.items()}
# ---------------------------------------------------------------------------

START_MONO = time.monotonic()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def validate_controls(raw):
    """Coerce a flat dict (HTTP params or persisted state) into a clean Picamera2
    controls dict. Accepts both user keys (brightness, ae, awbmode) and the
    Picamera2 names (Brightness, AeEnable, AwbMode). Unknown/invalid keys are dropped."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, (cname, lo, hi, _default) in LIVE_CONTROLS.items():
        val = raw.get(key, raw.get(cname))
        if val not in (None, ""):
            try:
                out[cname] = max(lo, min(hi, float(val)))
            except (ValueError, TypeError):
                pass
    ae = raw.get("ae", raw.get("AeEnable"))
    if ae is not None:
        out["AeEnable"] = _as_bool(ae)
    exp = raw.get("exposure", raw.get("ExposureTime"))
    if exp not in (None, ""):
        try:
            out["ExposureTime"] = max(EXPOSURE_RANGE[0], min(EXPOSURE_RANGE[1], int(float(exp))))
        except (ValueError, TypeError):
            pass
    gain = raw.get("gain", raw.get("AnalogueGain"))
    if gain not in (None, ""):
        try:
            out["AnalogueGain"] = max(GAIN_RANGE[0], min(GAIN_RANGE[1], float(gain)))
        except (ValueError, TypeError):
            pass
    awb = raw.get("awb", raw.get("AwbEnable"))
    if awb is not None:
        out["AwbEnable"] = _as_bool(awb)
    mode = raw.get("awbmode", raw.get("AwbMode"))
    if mode is not None:
        if isinstance(mode, str) and mode.strip().lower() in AWB_MODES:
            out["AwbMode"] = AWB_MODES[mode.strip().lower()]
        else:
            try:
                if int(mode) in AWB_BY_VALUE:
                    out["AwbMode"] = int(mode)
            except (ValueError, TypeError):
                pass
    return out


class StreamingOutput(io.BufferedIOBase):
    """Holds the latest JPEG frame and wakes waiting client threads.

    Lives for the whole process. close() is a deliberate no-op so the object
    survives the camera being stopped/restarted during a resolution change.
    Also tracks recent frame timestamps for an actual-FPS metric.
    """

    def __init__(self):
        self.frame = None
        self.condition = Condition()
        self._times = deque()

    def writable(self):
        return True

    def write(self, buf):
        with self.condition:
            self.frame = buf
            now = time.monotonic()
            self._times.append(now)
            cutoff = now - 2.0  # keep ~2s of timestamps for the fps estimate
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            self.condition.notify_all()
        return len(buf)

    def fps(self):
        with self.condition:
            if len(self._times) < 2:
                return 0.0
            span = self._times[-1] - self._times[0]
            return round((len(self._times) - 1) / span, 1) if span > 0 else 0.0

    def close(self):  # keep usable across reconfigurations
        pass


class CameraManager:
    """Owns the Picamera2 device and serializes (re)configuration."""

    def __init__(self):
        self.lock = Lock()
        self.client_lock = Lock()
        self.stream_sem = BoundedSemaphore(MAX_STREAMS)
        self.active_streams = 0
        self.output = StreamingOutput()
        self.picam2 = Picamera2()
        (self.width, self.height, self.framerate,
         self.hflip, self.vflip, self.controls) = self._load()
        self._start()
        self._save()  # make sure the state file exists

    def _load(self):
        w, h = DEFAULT_SIZE
        fps = DEFAULT_FPS
        hflip = vflip = False
        ctrls = {}
        try:
            with open(CONFIG_PATH) as f:
                d = json.load(f)
            if (int(d["width"]), int(d["height"])) in ALLOWED:
                w, h = int(d["width"]), int(d["height"])
            saved_fps = int(d.get("framerate", fps))
            if MIN_FPS <= saved_fps <= MAX_FPS:
                fps = saved_fps
            hflip = _as_bool(d.get("hflip", False))
            vflip = _as_bool(d.get("vflip", False))
            ctrls = validate_controls(d.get("controls", {}))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return w, h, fps, hflip, vflip, ctrls

    def _save(self):
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"width": self.width, "height": self.height,
                           "framerate": self.framerate, "hflip": self.hflip,
                           "vflip": self.vflip, "controls": self.controls}, f)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            logging.warning("could not save %s: %s", CONFIG_PATH, e)

    def _start(self):
        cfg = self.picam2.create_video_configuration(
            main={"size": (self.width, self.height)},
            controls={"FrameRate": self.framerate},
            transform=Transform(hflip=self.hflip, vflip=self.vflip),
        )
        self.picam2.configure(cfg)
        self.picam2.start_recording(JpegEncoder(), FileOutput(self.output))
        if self.controls:
            try:
                self.picam2.set_controls(self.controls)
            except Exception as e:  # noqa: BLE001 - bad persisted controls shouldn't crash
                logging.warning("could not apply saved controls %s: %s", self.controls, e)
        logging.info("Streaming at %dx%d @ %dfps (hflip=%s vflip=%s)",
                     self.width, self.height, self.framerate, self.hflip, self.vflip)

    def set_resolution(self, width, height, framerate=None):
        if (width, height) not in ALLOWED:
            raise ValueError("%dx%d is not an allowed resolution" % (width, height))
        if framerate is None:
            fps = self.framerate
        else:
            fps = max(MIN_FPS, min(MAX_FPS, int(framerate)))
        with self.lock:
            if (width, height, fps) == (self.width, self.height, self.framerate):
                return
            logging.info("Reconfiguring -> %dx%d @ %dfps", width, height, fps)
            self.picam2.stop_recording()
            self.width, self.height, self.framerate = width, height, fps
            self._start()
            self._save()

    def update_controls(self, hflip=None, vflip=None, controls=None):
        """Apply transform (flip) and/or live controls. Flip changes require a
        stop->reconfigure->start; live controls apply without a restart."""
        with self.lock:
            need_reconfig = False
            if hflip is not None and bool(hflip) != self.hflip:
                self.hflip = bool(hflip)
                need_reconfig = True
            if vflip is not None and bool(vflip) != self.vflip:
                self.vflip = bool(vflip)
                need_reconfig = True
            if controls:
                self.controls.update(controls)
            if need_reconfig:
                logging.info("Reconfiguring -> hflip=%s vflip=%s", self.hflip, self.vflip)
                self.picam2.stop_recording()
                self._start()  # reapplies self.controls too
            elif controls:
                try:
                    self.picam2.set_controls(controls)
                except Exception as e:  # noqa: BLE001 - surface as a 400 to the client
                    raise ValueError("camera rejected controls: %s" % e)
            self._save()

    def reset_controls(self):
        """Revert all camera controls and flip to their defaults. Live controls are
        set back explicitly (the camera retains the last value otherwise); flip is
        cleared via a reconfigure when needed."""
        defaults = {cname: dflt for (cname, _lo, _hi, dflt) in LIVE_CONTROLS.values()}
        defaults["AeEnable"] = True
        defaults["AwbEnable"] = True
        defaults["AwbMode"] = AWB_MODES["auto"]
        with self.lock:
            need_reconfig = self.hflip or self.vflip
            self.hflip = self.vflip = False
            self.controls = {}  # persist no overrides -> native defaults on restart
            if need_reconfig:
                logging.info("Resetting controls -> defaults (with reconfigure)")
                self.picam2.stop_recording()
                self._start()
            else:
                logging.info("Resetting controls -> defaults")
            try:
                self.picam2.set_controls(defaults)
            except Exception as e:  # noqa: BLE001 - surface as a 400 to the client
                raise ValueError("camera rejected reset: %s" % e)
            self._save()

    def _controls_view(self):
        c = self.controls
        return {
            "brightness": c.get("Brightness", LIVE_CONTROLS["brightness"][3]),
            "contrast": c.get("Contrast", LIVE_CONTROLS["contrast"][3]),
            "saturation": c.get("Saturation", LIVE_CONTROLS["saturation"][3]),
            "sharpness": c.get("Sharpness", LIVE_CONTROLS["sharpness"][3]),
            "ae": c.get("AeEnable", True),
            "exposure": c.get("ExposureTime", 0),
            "gain": c.get("AnalogueGain", 0.0),
            "awb": c.get("AwbEnable", True),
            "awbmode": AWB_BY_VALUE.get(c.get("AwbMode", 0), "auto"),
            "hflip": self.hflip,
            "vflip": self.vflip,
        }

    def state(self):
        return {
            "width": self.width,
            "height": self.height,
            "framerate": self.framerate,
            "current": "%dx%d" % (self.width, self.height),
            "options": ["%dx%d" % (w, h) for (w, h) in ALLOWED],
            "controls": self._controls_view(),
            "awbmodes": list(AWB_MODES.keys()),
        }

    def stop(self):
        with self.lock:
            try:
                self.picam2.stop_recording()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass


manager = None  # set in main()


def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _mem_available_kb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def health_snapshot():
    return {
        "ok": True,
        "uptime_s": round(time.monotonic() - START_MONO, 1),
        "fps": manager.output.fps(),
        "clients": manager.active_streams,
        "max_clients": MAX_STREAMS,
        "temp_c": _cpu_temp(),
        "mem_available_kb": _mem_available_kb(),
        "resolution": "%dx%d" % (manager.width, manager.height),
        "framerate": manager.framerate,
    }


PAGE_TMPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>__HOST__ camera</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif;text-align:center}
 header{background:#000;padding:.5rem;display:flex;gap:.7rem;align-items:center;
        justify-content:center;flex-wrap:wrap}
 select,input[type=number]{font-size:1rem;padding:.3rem .5rem;border-radius:.4rem;
        border:1px solid #444;background:#222;color:#eee}
 button{font-size:1rem;padding:.3rem .6rem;border-radius:.4rem;border:1px solid #444;
        background:#333;color:#eee;cursor:pointer}
 img{max-width:100%;height:auto;display:block;margin:0 auto;background:#000}
 #msg{font-size:.85rem;color:#9c9;min-width:11ch}
 details{background:#000;color:#eee;border-top:1px solid #222}
 summary{cursor:pointer;padding:.4rem;list-style:none}
 .ctrls{display:flex;gap:1rem;flex-wrap:wrap;justify-content:center;padding:.5rem}
 .ctrls label{display:flex;gap:.4rem;align-items:center;font-size:.85rem}
</style></head>
<body>
<header>
 <span>&#128247; __HOST__</span>
 <label>Resolution <select id="res"></select></label>
 <a href="/snapshot.jpg" target="_blank" style="color:#9cf">snapshot</a>
 <span id="msg"></span>
</header>
<details>
 <summary>Camera controls</summary>
 <div class="ctrls">
  <label>Brightness <input type="range" id="brightness" min="-1" max="1" step="0.1"></label>
  <label>Contrast <input type="range" id="contrast" min="0" max="4" step="0.1"></label>
  <label>Saturation <input type="range" id="saturation" min="0" max="4" step="0.1"></label>
  <label>Sharpness <input type="range" id="sharpness" min="0" max="4" step="0.1"></label>
  <label><input type="checkbox" id="hflip"> Flip H</label>
  <label><input type="checkbox" id="vflip"> Flip V</label>
  <label><input type="checkbox" id="ae"> Auto exposure</label>
  <label>Exposure µs <input type="number" id="exposure" min="100" max="1000000" step="100" style="width:8ch"></label>
  <label>Gain <input type="number" id="gain" min="1" max="16" step="0.1" style="width:5ch"></label>
  <label><input type="checkbox" id="awb"> Auto WB</label>
  <label>WB mode <select id="awbmode"></select></label>
  <button type="button" id="reset">Reset to defaults</button>
 </div>
</details>
<img id="stream" src="/stream.mjpg" alt="camera stream"/>
<script>
const sel=document.getElementById('res'),msg=document.getElementById('msg'),img=document.getElementById('stream');
const opts=__OPTIONS__,cur=__CURRENT__,curfps=__FPS__,ctl=__CONTROLS__,awbmodes=__AWBMODES__;
for(const o of opts){const e=document.createElement('option');e.value=o;e.textContent=o;if(o===cur)e.selected=true;sel.appendChild(e);}
msg.textContent=cur+' @ '+curfps+'fps';
sel.addEventListener('change',async()=>{
  const v=sel.value;sel.disabled=true;msg.textContent='switching to '+v+'...';
  try{
    const r=await fetch('/set_resolution?res='+encodeURIComponent(v),{method:'POST'});
    const j=await r.json();
    if(j.ok){msg.textContent=v+' @ '+j.framerate+'fps';img.src='/stream.mjpg?_='+Date.now();}
    else{msg.textContent='error: '+(j.error||'failed');}
  }catch(e){msg.textContent='error: '+e;}
  finally{sel.disabled=false;}
});
// --- camera controls ---
const am=document.getElementById('awbmode');
for(const m of awbmodes){const e=document.createElement('option');e.value=m;e.textContent=m;am.appendChild(e);}
function setVal(id,v){const el=document.getElementById(id);if(el.type==='checkbox')el.checked=!!v;else el.value=v;}
for(const k of ['brightness','contrast','saturation','sharpness','exposure','gain','ae','awb','hflip','vflip'])setVal(k,ctl[k]);
am.value=ctl.awbmode;
async function send(field,value){
  msg.textContent='applying '+field+'...';
  try{
    const r=await fetch('/set_controls?'+field+'='+encodeURIComponent(value),{method:'POST'});
    const j=await r.json();
    if(j.ok){msg.textContent=field+' set';if(field==='hflip'||field==='vflip')img.src='/stream.mjpg?_='+Date.now();}
    else{msg.textContent='error: '+(j.error||'failed');}
  }catch(e){msg.textContent='error: '+e;}
}
for(const id of ['brightness','contrast','saturation','sharpness'])
  document.getElementById(id).addEventListener('change',e=>send(id,e.target.value));
for(const id of ['exposure','gain'])
  document.getElementById(id).addEventListener('change',e=>send(id,e.target.value));
for(const id of ['hflip','vflip','ae','awb'])
  document.getElementById(id).addEventListener('change',e=>send(id,e.target.checked?1:0));
am.addEventListener('change',e=>send('awbmode',e.target.value));
document.getElementById('reset').addEventListener('click',async()=>{
  msg.textContent='resetting...';
  try{
    const r=await fetch('/set_controls?reset=1',{method:'POST'});
    const j=await r.json();
    if(j.ok){
      const c=j.controls;
      for(const k of ['brightness','contrast','saturation','sharpness','exposure','gain','ae','awb','hflip','vflip'])setVal(k,c[k]);
      am.value=c.awbmode;
      msg.textContent='defaults restored';img.src='/stream.mjpg?_='+Date.now();
    }else{msg.textContent='error: '+(j.error||'failed');}
  }catch(e){msg.textContent='error: '+e;}
});
</script>
</body></html>"""


def page_html():
    st = manager.state()
    return (PAGE_TMPL
            .replace("__HOST__", socket.gethostname())
            .replace("__OPTIONS__", json.dumps(st["options"]))
            .replace("__CURRENT__", json.dumps(st["current"]))
            .replace("__FPS__", str(st["framerate"]))
            .replace("__CONTROLS__", json.dumps(st["controls"]))
            .replace("__AWBMODES__", json.dumps(st["awbmodes"])))


class StreamingHandler(server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_params(self, u):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        return parse_qs(body) or parse_qs(u.query)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self.send_response(301)
            self.send_header("Location", "/index.html")
            self.end_headers()
        elif u.path == "/index.html":
            self._html(page_html())
        elif u.path == "/config":
            self._json(manager.state())
        elif u.path == "/healthz":
            self._json(health_snapshot())
        elif u.path == "/snapshot.jpg":
            self._snapshot()
        elif u.path == "/stream.mjpg":
            self._stream()
        elif u.path in ("/set_resolution", "/set_controls"):
            self.send_error(405, "use POST")
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/set_resolution":
            self._set(self._body_params(u))
        elif u.path == "/set_controls":
            self._set_controls(self._body_params(u))
        else:
            self.send_error(404)

    def _set(self, params):
        try:
            if "res" in params:
                w_s, h_s = params["res"][0].lower().split("x")
                width, height = int(w_s), int(h_s)
            else:
                width, height = int(params["width"][0]), int(params["height"][0])
            fps_raw = params.get("fps", [None])[0]
            fps = int(fps_raw) if fps_raw not in (None, "") else None
        except (KeyError, IndexError, ValueError):
            self._json({"ok": False,
                        "error": "use ?res=WIDTHxHEIGHT (e.g. 1280x720), optional &fps=N",
                        "options": manager.state()["options"]}, 400)
            return
        try:
            manager.set_resolution(width, height, fps)
        except ValueError as e:
            self._json({"ok": False, "error": str(e),
                        "options": manager.state()["options"]}, 400)
            return
        self._json({"ok": True, **manager.state()})

    def _set_controls(self, params):
        flat = {k: v[0] for k, v in params.items() if v}
        if _as_bool(flat.get("reset", "")):
            try:
                manager.reset_controls()
            except ValueError as e:
                self._json({"ok": False, "error": str(e), **manager.state()}, 400)
                return
            self._json({"ok": True, **manager.state()})
            return
        controls = validate_controls(flat)
        hflip = _as_bool(flat["hflip"]) if "hflip" in flat else None
        vflip = _as_bool(flat["vflip"]) if "vflip" in flat else None
        if "rotate" in flat:
            try:
                r = int(flat["rotate"]) % 360
                if r == 180:
                    hflip = vflip = True
                elif r == 0:
                    hflip = vflip = False
            except ValueError:
                pass
        if not controls and hflip is None and vflip is None:
            self._json({"ok": False, "error": "no recognized controls in request",
                        **manager.state()}, 400)
            return
        try:
            manager.update_controls(hflip=hflip, vflip=vflip, controls=controls)
        except ValueError as e:
            self._json({"ok": False, "error": str(e), **manager.state()}, 400)
            return
        self._json({"ok": True, **manager.state()})

    def _snapshot(self):
        with manager.output.condition:
            frame = manager.output.frame
        if not frame:
            self.send_error(503, "no frame available yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-cache, private")
        self.end_headers()
        self.wfile.write(frame)

    def _stream(self):
        if not manager.stream_sem.acquire(blocking=False):
            self.send_error(503, "too many concurrent streams")
            return
        with manager.client_lock:
            manager.active_streams += 1
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        out = manager.output
        try:
            while True:
                with out.condition:
                    out.condition.wait()
                    frame = out.frame
                if frame is None:
                    continue
                self.wfile.write(b"--FRAME\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001 - keep server alive on client errors
            logging.warning("stream client %s ended: %s", self.client_address, e)
        finally:
            with manager.client_lock:
                manager.active_streams -= 1
            manager.stream_sem.release()


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global manager
    manager = CameraManager()
    try:
        srv = StreamingServer((BIND, PORT), StreamingHandler)
        logging.info("Serving MJPEG on http://%s:%d/  (bind=%s)",
                     socket.gethostname(), PORT, BIND or "0.0.0.0")
        srv.serve_forever()
    finally:
        manager.stop()
        logging.info("Camera stopped")


if __name__ == "__main__":
    main()
