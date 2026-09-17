#!/usr/bin/env python3
"""boxen display daemon - owns the e-Paper panel, keeps it awake for partial
refresh, and serves a tiny local HTTP API for button gestures and agent
status pushes. Replaces the old one-off prototype_ui.py / button_*.py
scripts, which each re-init'd the panel (slow, full-flash) on every call.

Endpoints (127.0.0.1:8500):
  GET  /event/<single|double|long>  (used by PiSugar's button_shell - see
                                      below for why this is GET+path, not a
                                      POST body)
  POST /event  {"button": "single"|"double"|"long"}  (equivalent, JSON body)
  POST /status {"state": "idle"|"running"|"error", "task": "..."}
  GET  /health
"""
import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hud
import screens

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("boxen-display")

HOST, PORT = "127.0.0.1", 8500
FULL_REFRESH_EVERY = 12  # partial updates between full refreshes, to limit ghosting
CLOCK_TICK_SECONDS = 5


class DisplayDaemon:
    def __init__(self):
        from waveshare_epd import epd2in13_V3
        self.epd = epd2in13_V3.EPD()
        self.lock = threading.RLock()
        self.state = {
            "screen": "idle",
            "agent_state": "idle",
            "agent_task": "",
            "hostname": os.uname()[1],
            "clock": datetime.now().strftime("%H:%M:%S"),
        }
        self.partial_count = 0
        self._stop = threading.Event()

    def start(self):
        with self.lock:
            self.epd.init()
            self.epd.Clear(0xFF)
            image, draw = hud.new_canvas()
            screens.render_boot(draw, self.state)
            buf = self.epd.getbuffer(image)
            self.epd.display(buf)
            log.info("boot screen shown")
        time.sleep(1.5)
        with self.lock:
            self._render(force_full=True)
        threading.Thread(target=self._clock_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        with self.lock:
            self.epd.sleep()

    def _render(self, force_full=False):
        screen = self.state["screen"]
        if screen in screens.CYCLE:
            screens.set_screen_index(screens.CYCLE.index(screen), len(screens.CYCLE))
        else:
            screens.set_screen_index(None, None)

        image, draw = hud.new_canvas()
        screens.RENDERERS[screen](draw, self.state)
        buf = self.epd.getbuffer(image)

        if force_full or self.partial_count == 0:
            self.epd.display(buf)
            self.epd.displayPartBaseImage(buf)
            self.partial_count = 1
        else:
            self.epd.displayPartial(buf)
            self.partial_count += 1
            if self.partial_count >= FULL_REFRESH_EVERY:
                self.partial_count = 0
        log.info("rendered %s (partial_count=%d)", screen, self.partial_count)

    def handle_button(self, mode):
        with self.lock:
            if mode == "single":
                self.state["screen"] = screens.HOME
                self._render()
            elif mode == "double":
                cur = self.state["screen"]
                cycle = screens.CYCLE
                idx = cycle.index(cur) if cur in cycle else -1
                self.state["screen"] = cycle[(idx + 1) % len(cycle)]
                self._render()
            elif mode == "long":
                self.state["screen"] = "shutdown"
                self._render(force_full=True)
                time.sleep(1)
                log.info("shutdown triggered via long-press")
                os.system("/sbin/shutdown -h now")
            else:
                log.warning("unknown button mode: %s", mode)

    def handle_status(self, agent_state, agent_task):
        with self.lock:
            self.state["agent_state"] = agent_state
            self.state["agent_task"] = agent_task
            if self.state["screen"] == "idle":
                self._render()

    def _clock_loop(self):
        while not self._stop.is_set():
            time.sleep(CLOCK_TICK_SECONDS)
            with self.lock:
                self.state["clock"] = datetime.now().strftime("%H:%M:%S")
                if self.state["screen"] == "idle":
                    self._render()


def make_handler(daemon: DisplayDaemon):
    class Handler(BaseHTTPRequestHandler):
        def _json_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}

        def _respond(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/event":
                data = self._json_body()
                mode = data.get("button")
                if mode in ("single", "double", "long"):
                    daemon.handle_button(mode)
                    self._respond(200, {"ok": True, "screen": daemon.state["screen"]})
                else:
                    self._respond(400, {"error": "bad button"})
            elif self.path == "/status":
                data = self._json_body()
                daemon.handle_status(data.get("state", "idle"), data.get("task", ""))
                self._respond(200, {"ok": True})
            else:
                self._respond(404, {"error": "not found"})

        def do_GET(self):
            if self.path == "/health":
                self._respond(200, {"ok": True, "screen": daemon.state["screen"]})
            elif self.path.startswith("/event/"):
                # GET variant, mode in the path rather than a JSON body - the
                # PiSugar button_shell setting strips quote characters from
                # its command string, so a JSON POST body doesn't survive
                # round-tripping through it. Path segment sidesteps that.
                mode = self.path[len("/event/"):]
                if mode in ("single", "double", "long"):
                    daemon.handle_button(mode)
                    self._respond(200, {"ok": True, "screen": daemon.state["screen"]})
                else:
                    self._respond(400, {"error": "bad button"})
            else:
                self._respond(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def main():
    daemon = DisplayDaemon()
    daemon.start()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(daemon))
    log.info("listening on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()


if __name__ == "__main__":
    main()
