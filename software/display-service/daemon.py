#!/usr/bin/env python3
"""Axon display daemon - owns the Whisplay color LCD + RGB LED, and serves
a tiny local HTTP API for button gestures and agent status pushes.

No partial-refresh dance here unlike the old e-ink version: this is a
normal LCD, so every render is just "build the frame, push the whole
thing" - much simpler than e-ink's base-image/partial/full-refresh-every-
N-updates juggling.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

import hud
import screens

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("axon-display")

HOST, PORT = "127.0.0.1", 8500

# broker-host is the "brain" - a stationary, wall-powered Pi 5 - vs. display-device which is
# meant to be picked up and carried, so it's not a reliable place to host the
# broker itself. See docs/PROTOTYPE-PWNAGOTCHI.md for the reasoning.
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "axon/agents/+/status"

# LED colors come from hud.STATUS_COLORS (dimmed - see hud.dim) rather than
# a separate mapping defined here, so the LED and the on-screen status dot
# can never drift into disagreeing about what a given state looks like.


class DisplayDaemon:
    def __init__(self):
        from whisplay import WhisplayBoard
        self.board = WhisplayBoard()
        self.board.set_backlight(70)
        self.lock = threading.RLock()
        self.state = {
            "screen": "idle",
            "agent_state": "idle",
            "agent_task": "",
            "hostname": os.uname()[1],
            "agents": {},  # source (hostname/session id) -> {state, task, ts}
        }
        self._stop = threading.Event()
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.mqtt.on_connect = self._mqtt_on_connect
        self.mqtt.on_message = self._mqtt_on_message

    def start(self):
        with self.lock:
            image, draw = hud.new_canvas()
            screens.render_boot(image, draw, self.state)
            self.board.draw_image(0, 0, hud.W, hud.H, hud.to_rgb565(image))
            log.info("boot screen shown")
        time.sleep(1.2)
        with self.lock:
            self._render()
        self._start_mqtt()

    def stop(self):
        self._stop.set()
        self.mqtt.loop_stop()
        with self.lock:
            self.board.set_rgb(0, 0, 0)
            self.board.cleanup()

    def _start_mqtt(self):
        try:
            self.mqtt.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self.mqtt.loop_start()
        except OSError as e:
            log.warning("mqtt connect to %s:%d failed: %s (will keep retrying)", MQTT_BROKER, MQTT_PORT, e)
            self.mqtt.loop_start()  # paho retries connection in the background loop

    def _mqtt_on_connect(self, client, userdata, connect_flags, reason_code, properties):
        log.info("mqtt connected to %s:%d (%s)", MQTT_BROKER, MQTT_PORT, reason_code)
        client.subscribe(MQTT_TOPIC)

    def _mqtt_on_message(self, client, userdata, msg):
        # topic shape: axon/agents/<source>/status
        parts = msg.topic.split("/")
        if len(parts) != 4:
            return
        source = parts[2]
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            log.warning("bad mqtt payload on %s: %r", msg.topic, msg.payload)
            return
        with self.lock:
            self.state["agents"][source] = {
                "state": payload.get("state", "idle"),
                "task": payload.get("task", ""),
                "ts": time.time(),
            }
            log.info("agent update: %s -> %s", source, payload)
            if self.state["screen"] == "idle":
                self._render()

    def _update_led(self):
        bucket = hud.aggregate_status(self.state["agents"])
        color = hud.STATUS_EMPTY if bucket is None else hud.status_color(bucket)
        self.board.set_rgb(*hud.dim(color))

    def _render(self):
        screen = self.state["screen"]
        image, draw = hud.new_canvas()
        screens.RENDERERS[screen](image, draw, self.state)
        self.board.draw_image(0, 0, hud.W, hud.H, hud.to_rgb565(image))
        if screen == "idle":
            self._update_led()
        log.info("rendered %s", screen)

    def handle_button(self, mode):
        with self.lock:
            if mode == "single":
                self.state["screen"] = screens.HOME
                self._render()
            elif mode == "double":
                # No page cycle anymore (single screen now) - just refresh.
                cycle = screens.CYCLE
                if cycle:
                    cur = self.state["screen"]
                    idx = cycle.index(cur) if cur in cycle else -1
                    self.state["screen"] = cycle[(idx + 1) % len(cycle)]
                self._render()
            elif mode == "long":
                self.state["screen"] = "shutdown"
                self.board.set_rgb(255, 80, 0)
                self._render()
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
