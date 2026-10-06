#!/usr/bin/env python3
"""W4/W5 inspection service with PostgreSQL persistence and idempotency."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import psycopg2
from psycopg2 import IntegrityError


EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ALLOWED_TYPES = {"status", "anomaly", "test"}
MAX_BODY = 4096


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_db_config():
    required = [
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ]

    if not all(os.environ.get(key) for key in required):
        return None

    return {
        key: os.environ[key]
        for key in required
    }


def db_connect(config):
    return psycopg2.connect(
        host=config["DB_HOST"],
        port=int(config["DB_PORT"]),
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        sslmode="verify-full",
        sslrootcert="/etc/inspection/rds-ca.pem",
        connect_timeout=5,
    )


def ensure_schema(config):
    conn = db_connect(config)

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id VARCHAR(64) PRIMARY KEY,
                        device_id VARCHAR(32) NOT NULL,
                        observed_at TEXT NOT NULL,
                        type VARCHAR(16) NOT NULL,
                        note VARCHAR(200),
                        received_at TEXT NOT NULL
                    )
                    """
                )
    finally:
        conn.close()


def validate_event(event):
    if not isinstance(event, dict):
        return False, "body"

    allowed = {"event_id", "device_id", "observed_at", "type", "note"}

    extra = set(event) - allowed
    if extra:
        return False, sorted(extra)[0]

    for field in ("event_id", "device_id", "observed_at", "type"):
        if field not in event:
            return False, field

    event_id = event["event_id"]
    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        return False, "event_id"

    device_id = event["device_id"]
    if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
        return False, "device_id"

    observed_at = event["observed_at"]
    if not isinstance(observed_at, str):
        return False, "observed_at"

    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False, "observed_at"
    except ValueError:
        return False, "observed_at"

    event_type = event["type"]
    if not isinstance(event_type, str) or event_type not in ALLOWED_TYPES:
        return False, "type"

    if "note" in event:
        note = event["note"]
        if not isinstance(note, str) or len(note) > 200:
            return False, "note"

    return True, None


def event_for_db(event, received_at):
    return (
        event["event_id"],
        event["device_id"],
        event["observed_at"],
        event["type"],
        event.get("note"),
        received_at,
    )


def make_server(version_file, port=8080, reporter_token=None, operator_token=None):
    version = Path(version_file).read_text(encoding="utf-8").strip()

    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")

    started = utc_now()

    if reporter_token is None:
        reporter_token = os.environ.get("REPORTER_TOKEN")

    if operator_token is None:
        operator_token = os.environ.get("OPERATOR_TOKEN")

    db_config = load_db_config()

    if db_config:
        try:
            ensure_schema(db_config)
        except Exception:
            # Service must still start even when the DB is temporarily unavailable.
            pass

    class Handler(BaseHTTPRequestHandler):

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def auth(self, expected):
            header = self.headers.get("Authorization")

            if not header or not header.startswith("Bearer "):
                self.send_json(401, {"error": "unauthorized"})
                return False

            token = header[7:]

            if not expected or token != expected:
                self.send_json(403, {"error": "forbidden"})
                return False

            return True

        def do_GET(self):
            if self.path == "/health":
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "inspection",
                        "version": version,
                        "started_at": started,
                        "auth_configured": bool(
                            reporter_token and operator_token
                        ),
                        "db_configured": db_config is not None,
                    },
                )
                return

            if self.path == "/":
                page = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Inspection Events</title>
</head>
<body>
<h1>Inspection Events</h1>
<label>
Operator token:
<input id="token" type="password">
</label>
<button id="load">Load</button>
<pre id="output"></pre>

<script>
const tokenInput = document.getElementById("token");
const output = document.getElementById("output");

document.getElementById("load").addEventListener("click", async () => {
    const token = tokenInput.value;

    const response = await fetch("/events", {
        headers: {
            "Authorization": "Bearer " + token
        }
    });

    const data = await response.json();

    output.textContent = JSON.stringify(data, null, 2);
});
</script>
</body>
</html>"""

                data = page.encode("utf-8")

                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return

            if self.path == "/events":
                if not self.auth(operator_token):
                    return

                if not db_config:
                    self.send_json(200, [])
                    return

                try:
                    conn = db_connect(db_config)

                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT event_id,
                                       device_id,
                                       observed_at,
                                       type,
                                       note,
                                       received_at
                                FROM events
                                ORDER BY received_at DESC
                                LIMIT 50
                                """
                            )

                            rows = cur.fetchall()

                    finally:
                        conn.close()

                    events = []

                    for row in rows:
                        events.append(
                            {
                                "event_id": row[0],
                                "device_id": row[1],
                                "observed_at": row[2],
                                "type": row[3],
                                **({"note": row[4]} if row[4] is not None else {}),
                                "received_at": row[5],
                            }
                        )

                    self.send_json(200, events)

                except Exception:
                    self.send_json(
                        503,
                        {"error": "database_unavailable"}
                    )

                return

            if self.path.startswith("/events/"):
                if not self.auth(operator_token):
                    return

                event_id = self.path[len("/events/"):]

                if not db_config:
                    self.send_json(
                        404,
                        {"error": "not_found", "field": "event_id"}
                    )
                    return

                try:
                    conn = db_connect(db_config)

                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT event_id,
                                       device_id,
                                       observed_at,
                                       type,
                                       note,
                                       received_at
                                FROM events
                                WHERE event_id = %s
                                """,
                                (event_id,),
                            )

                            row = cur.fetchone()

                    finally:
                        conn.close()

                    if row is None:
                        self.send_json(
                            404,
                            {"error": "not_found", "field": "event_id"}
                        )
                        return

                    result = {
                        "event_id": row[0],
                        "device_id": row[1],
                        "observed_at": row[2],
                        "type": row[3],
                        **({"note": row[4]} if row[4] is not None else {}),
                        "received_at": row[5],
                    }

                    self.send_json(200, result)

                except Exception:
                    self.send_json(
                        503,
                        {"error": "database_unavailable"}
                    )

                return

            self.send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/events":
                self.send_json(404, {"error": "not_found"})
                return

            if not self.auth(reporter_token):
                return

            content_type = self.headers.get("Content-Type", "")

            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self.send_json(400, {"error": "invalid_content_type", "field": "Content-Type"})
                return

            content_length = self.headers.get("Content-Length")

            try:
                length = int(content_length or "0")
            except ValueError:
                self.send_json(400, {"error": "invalid_content_length", "field": "Content-Length"})
                return

            if length > MAX_BODY:
                self.send_json(400, {"error": "body_too_large", "field": "body"})
                return

            try:
                raw = self.rfile.read(length)
            except Exception:
                self.send_json(400, {"error": "invalid_body", "field": "body"})
                return

            if len(raw) > MAX_BODY:
                self.send_json(400, {"error": "body_too_large", "field": "body"})
                return

            try:
                event = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json(400, {"error": "invalid_json", "field": "body"})
                return

            valid, field = validate_event(event)

            if not valid:
                self.send_json(
                    400,
                    {"error": "invalid_event", "field": field}
                )
                return

            if not db_config:
                self.send_json(
                    503,
                    {"error": "database_unavailable"}
                )
                return

            received_at = utc_now()

            try:
                conn = db_connect(db_config)

                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO events
                                    (event_id, device_id, observed_at,
                                     type, note, received_at)
                                VALUES
                                    (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (event_id) DO NOTHING
                                RETURNING event_id
                                """,
                                event_for_db(event, received_at),
                            )

                            inserted = cur.fetchone()

                            if inserted:
                                created = dict(event)
                                created["received_at"] = received_at
                                self.send_json(201, created)
                                return

                            cur.execute(
                                """
                                SELECT event_id,
                                       device_id,
                                       observed_at,
                                       type,
                                       note,
                                       received_at
                                FROM events
                                WHERE event_id = %s
                                """,
                                (event["event_id"],),
                            )

                            existing = cur.fetchone()

                finally:
                    conn.close()

                if existing is None:
                    self.send_json(
                        409,
                        {"error": "event_conflict", "field": "event_id"}
                    )
                    return

                existing_event = {
                    "event_id": existing[0],
                    "device_id": existing[1],
                    "observed_at": existing[2],
                    "type": existing[3],
                    **({"note": existing[4]} if existing[4] is not None else {}),
                }

                incoming_event = dict(event)

                if existing_event == incoming_event:
                    existing_event["received_at"] = existing[5]
                    self.send_json(200, existing_event)
                else:
                    self.send_json(
                        409,
                        {"error": "event_conflict", "field": "event_id"}
                    )

            except Exception:
                self.send_json(
                    503,
                    {"error": "database_unavailable"}
                )

        def log_message(self, fmt, *args):
            # Never log request paths, bodies, headers, tokens or DB credentials.
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
