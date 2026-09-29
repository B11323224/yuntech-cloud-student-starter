#!/usr/bin/env python3
"""W3 supplied inspection-service prototype; extend routes in later Sprints."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
from threading import RLock
from urllib.parse import unquote, urlsplit


def make_server(version_file, port=8080, reporter_token=None, operator_token=None):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    reporter_token = os.environ.get("REPORTER_TOKEN") if reporter_token is None else reporter_token
    operator_token = os.environ.get("OPERATOR_TOKEN") if operator_token is None else operator_token
    auth_configured = bool(reporter_token and operator_token and reporter_token != operator_token)
    events = {}
    events_lock = RLock()
    required_fields = {"event_id", "device_id", "observed_at", "type"}
    allowed_fields = required_fields | {"note"}
    identifier = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
    device_identifier = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
    event_types = {"status", "anomaly", "test"}
    page = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspection events</title>
<h1>Inspection events</h1>
<label>Operator token <input id="token" type="password" autocomplete="off"></label>
<button id="load" type="button">Load events</button>
<p id="status" role="status"></p>
<ol id="events"></ol>
<script>
const tokenInput = document.getElementById("token");
const statusNode = document.getElementById("status");
const list = document.getElementById("events");
document.getElementById("load").addEventListener("click", async () => {
  const token = tokenInput.value;
  tokenInput.value = "";
  list.replaceChildren();
  try {
    const response = await fetch("/events", {
      headers: {"Authorization": "Bearer " + token}
    });
    const payload = await response.json();
    if (!response.ok) throw new Error("Request rejected (" + response.status + ")");
    for (const event of payload.events) {
      const item = document.createElement("li");
      item.textContent = event.event_id + " | " + event.device_id + " | " +
        event.type + " | " + event.observed_at + " | " + (event.note || "");
      list.appendChild(item);
    }
    statusNode.textContent = "Loaded " + payload.events.length + " events";
  } catch (error) {
    statusNode.textContent = error.message;
  }
});
</script>
</html>"""

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        protocol_version = "HTTP/1.0"

        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status, error, field=""):
            self.send_json(status, {"error": error, "field": field})

        def role_for_request(self):
            scheme, separator, token = self.headers.get("Authorization", "").partition(" ")
            if not separator or scheme.lower() != "bearer" or not token:
                return None
            if reporter_token and hmac.compare_digest(token, reporter_token):
                return "reporter"
            if operator_token and hmac.compare_digest(token, operator_token):
                return "operator"
            return None

        def validate_event(self, event):
            if not isinstance(event, dict):
                return "body"
            extra = sorted(set(event) - allowed_fields)
            if extra:
                return extra[0]
            missing = sorted(required_fields - set(event))
            if missing:
                return missing[0]
            if not isinstance(event["event_id"], str) or not identifier.fullmatch(event["event_id"]):
                return "event_id"
            if not isinstance(event["device_id"], str) or not device_identifier.fullmatch(event["device_id"]):
                return "device_id"
            if not isinstance(event["observed_at"], str):
                return "observed_at"
            try:
                observed = datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00"))
            except ValueError:
                return "observed_at"
            if observed.tzinfo is None or observed.utcoffset() is None:
                return "observed_at"
            if not isinstance(event["type"], str) or event["type"] not in event_types:
                return "type"
            if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 200):
                return "note"
            return None

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {"status": "ok", "service": "inspection", "version": version,
                                     "started_at": started, "auth_configured": auth_configured})
                return
            if path == "/":
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/events" or re.fullmatch(r"/events/[^/]+", path):
                role = self.role_for_request()
                if role is None:
                    self.send_error_json(401, "unauthorized")
                    return
                if role != "operator":
                    self.send_error_json(403, "forbidden")
                    return
                if path == "/events":
                    with events_lock:
                        recent = list(events.values())[-50:]
                    self.send_json(200, {"events": list(reversed(recent))})
                    return
                event_id = unquote(path.removeprefix("/events/"))
                with events_lock:
                    event = events.get(event_id)
                if event is None:
                    self.send_error_json(404, "not_found", "event_id")
                    return
                self.send_json(200, event)
                return
            self.send_error_json(404, "not_found")

        def do_POST(self):
            if urlsplit(self.path).path != "/events":
                self.send_error_json(404, "not_found")
                return
            role = self.role_for_request()
            if role is None:
                self.send_error_json(401, "unauthorized")
                return
            if role != "reporter":
                self.send_error_json(403, "forbidden")
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error_json(400, "invalid_content_type", "Content-Type")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error_json(400, "invalid_content_length", "Content-Length")
                return
            if length < 0 or length > 4096:
                self.send_error_json(400, "invalid_body_size", "body")
                return
            try:
                event = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error_json(400, "invalid_json", "body")
                return
            invalid_field = self.validate_event(event)
            if invalid_field:
                self.send_error_json(400, "invalid_event", invalid_field)
                return
            event_id = event["event_id"]
            stored = dict(event)
            stored["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            with events_lock:
                if event_id in events:
                    duplicate = True
                else:
                    events[event_id] = stored
                    duplicate = False
            if duplicate:
                self.send_error_json(409, "duplicate_event", "event_id")
                return
            self.send_json(201, stored)

        def log_message(self, _fmt, *_args):
            _ = (_fmt, _args)
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
