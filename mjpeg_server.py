#!/usr/bin/python3
"""
MJPEG camera stream for the Raspberry Pi Camera Module 2 (IMX219) on a Pi Zero 2 W,
using Picamera2 -- with live, dynamic resolution switching.

  Browser UI:         http://<pi>:8000/            (resolution dropdown in the header)
  Raw MJPEG stream:   http://<pi>:8000/stream.mjpg
  Current config:     http://<pi>:8000/config      (JSON)
  Change resolution:  http://<pi>:8000/set_resolution?res=1280x720   (GET or POST)
                      optionally add &fps=15

The selected resolution is applied to the live camera (no service restart) and saved
to stream-config.json next to this script, so it persists across restarts/reboots.
Resolutions are whitelisted (see ALLOWED) to stay safe on the 512 MB board.
"""

import io
import json
import logging
import os
import socket
import socketserver
from http import server
from threading import Condition, Lock
from urllib.parse import urlparse, parse_qs

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

# ---- Config ---------------------------------------------------------------
PORT = 8000
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
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "stream-config.json")
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


class StreamingOutput(io.BufferedIOBase):
    """Holds the latest JPEG frame and wakes waiting client threads.

    Lives for the whole process. close() is a deliberate no-op so the object
    survives the camera being stopped/restarted during a resolution change.
    """

    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def writable(self):
        return True

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()
        return len(buf)

    def close(self):  # keep usable across reconfigurations
        pass


class CameraManager:
    """Owns the Picamera2 device and serializes (re)configuration."""

    def __init__(self):
        self.lock = Lock()
        self.output = StreamingOutput()
        self.picam2 = Picamera2()
        self.width, self.height, self.framerate = self._load()
        self._start()
        self._save()  # make sure the state file exists

    def _load(self):
        w, h = DEFAULT_SIZE
        fps = DEFAULT_FPS
        try:
            with open(CONFIG_PATH) as f:
                d = json.load(f)
            if (int(d["width"]), int(d["height"])) in ALLOWED:
                w, h = int(d["width"]), int(d["height"])
            saved_fps = int(d.get("framerate", fps))
            if MIN_FPS <= saved_fps <= MAX_FPS:
                fps = saved_fps
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        return w, h, fps

    def _save(self):
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"width": self.width, "height": self.height,
                           "framerate": self.framerate}, f)
            os.replace(tmp, CONFIG_PATH)
        except OSError as e:
            logging.warning("could not save %s: %s", CONFIG_PATH, e)

    def _start(self):
        cfg = self.picam2.create_video_configuration(
            main={"size": (self.width, self.height)},
            controls={"FrameRate": self.framerate},
        )
        self.picam2.configure(cfg)
        self.picam2.start_recording(JpegEncoder(), FileOutput(self.output))
        logging.info("Streaming at %dx%d @ %dfps",
                     self.width, self.height, self.framerate)

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

    def state(self):
        return {
            "width": self.width,
            "height": self.height,
            "framerate": self.framerate,
            "current": "%dx%d" % (self.width, self.height),
            "options": ["%dx%d" % (w, h) for (w, h) in ALLOWED],
        }

    def stop(self):
        with self.lock:
            try:
                self.picam2.stop_recording()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass


manager = None  # set in main()


PAGE_TMPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>__HOST__ camera</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif;text-align:center}
 header{background:#000;padding:.5rem;display:flex;gap:.7rem;align-items:center;
        justify-content:center;flex-wrap:wrap}
 select{font-size:1rem;padding:.3rem .5rem;border-radius:.4rem;border:1px solid #444;
        background:#222;color:#eee}
 img{max-width:100%;height:auto;display:block;margin:0 auto;background:#000}
 #msg{font-size:.85rem;color:#9c9;min-width:11ch}
</style></head>
<body>
<header>
 <span>&#128247; __HOST__</span>
 <label>Resolution <select id="res"></select></label>
 <span id="msg"></span>
</header>
<img id="stream" src="/stream.mjpg" alt="camera stream"/>
<script>
const sel=document.getElementById('res'),msg=document.getElementById('msg'),img=document.getElementById('stream');
const opts=__OPTIONS__,cur=__CURRENT__,curfps=__FPS__;
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
</script>
</body></html>"""


def page_html():
    st = manager.state()
    return (PAGE_TMPL
            .replace("__HOST__", socket.gethostname())
            .replace("__OPTIONS__", json.dumps(st["options"]))
            .replace("__CURRENT__", json.dumps(st["current"]))
            .replace("__FPS__", str(st["framerate"])))


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
        elif u.path == "/set_resolution":
            self._set(parse_qs(u.query))
        elif u.path == "/stream.mjpg":
            self._stream()
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/set_resolution":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            params = parse_qs(body) or parse_qs(u.query)
            self._set(params)
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

    def _stream(self):
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


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global manager
    manager = CameraManager()
    try:
        srv = StreamingServer(("", PORT), StreamingHandler)
        logging.info("Serving MJPEG on http://%s:%d/  (resolution selector in the page)",
                     socket.gethostname(), PORT)
        srv.serve_forever()
    finally:
        manager.stop()
        logging.info("Camera stopped")


if __name__ == "__main__":
    main()
