import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.service import make_server


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
REPORTER = "reporter-test-token"
OPERATOR = "operator-test-token"


class W04ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        version = Path(self.temp.name) / "version"
        version.write_text("a" * 40, encoding="utf-8")
        self.server = make_server(version, port=0, reporter_token=REPORTER, operator_token=OPERATOR)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def request(self, method, path, payload=None, token=None, content_type="application/json"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read()) if "json" in response.headers.get("Content-Type", "") else response.read().decode()

    def fixture(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_health_reports_version_and_auth_configuration(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "a" * 40)
        self.assertTrue(body["auth_configured"])

    def test_reporter_can_create_and_operator_can_read_event(self):
        event = self.fixture("valid_event.json")
        status, created = self.request("POST", "/events", event, REPORTER)
        self.assertEqual(status, 201)
        self.assertTrue(created["received_at"].endswith("Z"))
        status, fetched = self.request("GET", "/events/" + event["event_id"], token=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(fetched, created)

    def test_authentication_precedes_authorization_and_validation(self):
        invalid = self.fixture("invalid_timezone.json")
        self.assertEqual(self.request("POST", "/events", invalid)[0], 401)
        self.assertEqual(self.request("POST", "/events", invalid, OPERATOR)[0], 403)
        self.assertEqual(self.request("POST", "/events", invalid, REPORTER)[0], 400)

    def test_fixture_validation_errors_identify_field(self):
        for name, field in (("invalid_timezone.json", "observed_at"),
                            ("invalid_extra_field.json", "unexpected")):
            status, body = self.request("POST", "/events", self.fixture(name), REPORTER)
            self.assertEqual(status, 400)
            self.assertEqual(body["field"], field)

    def test_non_string_event_type_is_rejected(self):
        event = self.fixture("valid_event.json")
        event["type"] = []
        status, body = self.request("POST", "/events", event, REPORTER)
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "type")

    def test_duplicate_event_is_conflict(self):
        event = self.fixture("valid_event.json")
        self.assertEqual(self.request("POST", "/events", event, REPORTER)[0], 201)
        status, body = self.request("POST", "/events", event, REPORTER)
        self.assertEqual(status, 409)
        self.assertEqual(body["field"], "event_id")

    def test_roles_content_type_and_unknown_event(self):
        event = self.fixture("valid_event.json")
        self.request("POST", "/events", event, REPORTER)
        self.assertEqual(self.request("GET", "/events", token=REPORTER)[0], 403)
        self.assertEqual(self.request("GET", "/events", token=OPERATOR)[0], 200)
        self.assertEqual(self.request("GET", "/events/not-found", token=OPERATOR)[0], 404)
        self.assertEqual(self.request("POST", "/events", event, REPORTER, "text/plain")[0], 400)

    def test_oversized_event_body_is_rejected(self):
        event = self.fixture("valid_event.json")
        event["note"] = "x" * 5000
        status, body = self.request("POST", "/events", event, REPORTER)
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "body")

    def test_display_uses_text_content_and_does_not_persist_token(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("?token=", page)


if __name__ == "__main__":
    unittest.main()