#!/usr/bin/env python3
"""Run the seven W4 HTTP contract cases without printing bearer tokens."""
import json
from pathlib import Path
import re
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SECRET_FILE = ROOT / ".local" / "app.env"


def load_secrets():
    if SECRET_FILE.is_symlink() or not SECRET_FILE.is_file():
        raise ValueError(".local/app.env is missing or unsafe")
    if stat.S_IMODE(SECRET_FILE.stat().st_mode) != 0o600:
        raise ValueError(".local/app.env must have mode 600")
    values = {}
    for line in SECRET_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key in {"REPORTER_TOKEN", "OPERATOR_TOKEN"}:
            values[key] = value
    reporter = values.get("REPORTER_TOKEN", "")
    operator = values.get("OPERATOR_TOKEN", "")
    if not reporter or not operator or reporter == operator:
        raise ValueError("app.env must contain two distinct non-empty tokens")
    return reporter, operator


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def send(base_url, method, path, payload=None, token=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=8)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"body": "<non-JSON response>"}
        return response.status, payload


def safe_body(body, tokens):
    rendered = json.dumps(body, ensure_ascii=True, sort_keys=True)
    for token in tokens:
        rendered = rendered.replace(token, "[REDACTED]")
    return rendered


def main():
    try:
        reporter, operator = load_secrets()
        raw_url = input("Service base URL (for example http://HOST): ").strip().rstrip("/")
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Enter only an http(s) base URL without path, query, or fragment")
        base_url = raw_url
        status, health = send(base_url, "GET", "/health")
        if status != 200 or not re.fullmatch(r"[0-9a-f]{40}", str(health.get("version", ""))):
            raise ValueError("Target did not return a valid /health version")
        print("version:", health["version"])
        print("Target:", parsed.netloc)
        if input("Type RUN W4 MATRIX to send one in-memory test event: ").strip() != "RUN W4 MATRIX":
            print("Cancelled; no matrix requests sent.")
            return 1

        valid = load_fixture("valid_event.json")
        run_suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        valid["event_id"] = valid["event_id"].rsplit("-", 1)[0] + "-" + run_suffix
        invalid_timezone = load_fixture("invalid_timezone.json")
        invalid_timezone["event_id"] = invalid_timezone["event_id"].rsplit("-", 1)[0] + "-" + run_suffix
        wrong_role = dict(valid)
        rows = [
            ("#1 reporter creates", 201, "POST", "/events", valid, reporter),
            ("#2 missing token", 401, "POST", "/events", valid, None),
            ("#3 operator posts", 403, "POST", "/events", wrong_role, operator),
            ("#4 timezone required", 400, "POST", "/events", invalid_timezone, reporter),
            ("#5 duplicate event", 409, "POST", "/events", valid, reporter),
            ("#6 reporter reads", 403, "GET", "/events", None, reporter),
            ("#7 operator reads", 200, "GET", "/events", None, operator),
        ]
        failures = 0
        for label, expected, method, path, body, token in rows:
            actual, response_body = send(base_url, method, path, body, token)
            passed = actual == expected
            if label == "#7 operator reads":
                passed = passed and any(item.get("event_id") == valid["event_id"]
                                        for item in response_body.get("events", []))
            failures += not passed
            print(f"{label}: HTTP {actual} {'OK' if passed else f'EXPECTED {expected}'} {safe_body(response_body, (reporter, operator))}")
        print(f"Matrix complete: {len(rows) - failures}/{len(rows)} passed")
        return 1 if failures else 0
    except (OSError, ValueError, URLError, json.JSONDecodeError) as exc:
        print("STOP:", type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())