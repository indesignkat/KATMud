"""katmud_lib.hub - the phone/web dashboard hub process (spec:
docs/superpowers/specs/2026-06-21-web-dashboard-design.md).

Stdlib-only, no Tkinter, no MUD connection. Reads webstate/*.json
(written once a second by each character's tick()) and reminders.json,
serves them as one aggregated JSON blob; writes webstate/<id>.cmd when
the phone submits a command (claimed and deleted by the character
process - see paths.claim_webcmd).
"""
import glob
import json
import os
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths

OFFLINE_AFTER_SECONDS = 10
LOCALHOST_FALLBACK = "127.0.0.1"
DEFAULT_PORT = 8732
DASHBOARD_HTML = os.path.join(os.path.dirname(__file__), "dashboard.html")


def resolve_bind_host(runner=subprocess.run):
    """The Tailscale interface's IPv4 address, or 127.0.0.1 if
    `tailscale` isn't installed/running. Binding the Tailscale IP
    (rather than 0.0.0.0) keeps POST /api/command - which can run MUD
    commands and release the deadman gate - off the rest of the LAN."""
    try:
        result = runner(["tailscale", "ip", "-4"],
                        capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return LOCALHOST_FALLBACK
    if result.returncode != 0:
        return LOCALHOST_FALLBACK
    first_line = result.stdout.strip().splitlines()[:1]
    return first_line[0].strip() if first_line else LOCALHOST_FALLBACK


def build_state(now=None):
    """Pure aggregation of every webstate/*.json + reminders.json into
    one dict for GET /api/state. No I/O side effects, no network -
    kept separate from the HTTP handler so it's testable directly."""
    if now is None:
        now = time.time()
    characters = []
    pattern = os.path.join(paths.WEBSTATE_DIR, "*.json")
    for path in sorted(glob.glob(pattern)):
        data, err = paths.load_json(path)
        if err:
            continue
        profile_id = os.path.splitext(os.path.basename(path))[0]
        char = dict(data)
        char["profile_id"] = profile_id
        char["offline"] = (now - data.get("updated", 0)) > \
            OFFLINE_AFTER_SECONDS
        characters.append(char)
    reminders_data, _ = paths.load_json(
        paths.REMINDERS_FILE, {"reminders": [], "next_id": 1})
    return {
        "characters": characters,
        "reminders": reminders_data.get("reminders", []),
    }


def is_hub_running(host, port, timeout=0.5):
    """True if something is already listening on host:port - used by
    the picker to decide whether it needs to spawn the hub before
    spawning the requested character."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/state":
            self._send_json(200, build_state())
            return
        if self.path == "/":
            try:
                with open(DASHBOARD_HTML, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/command":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        profile_id = body.get("profile_id", "")
        text = body.get("text", "")
        if not profile_id or not text:
            self._send_json(400, {"error": "profile_id and text required"})
            return
        paths.write_webcmd(profile_id, text)
        self._send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass  # dashboard is a quiet, low-traffic phone tool - no console spam


def run(host=None, port=DEFAULT_PORT):
    paths.ensure_dirs()
    if host is None:
        host = resolve_bind_host()
    server = ThreadingHTTPServer((host, port), _Handler)
    server.serve_forever()
