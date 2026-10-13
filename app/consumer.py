#!/usr/bin/env python3
"""AWS IoT MQTT consumer that validates device IDs and reports events."""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("inspection-consumer")


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def report_event(event, token):
    """POST one event to the local inspection API."""
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:8080/events",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            response.read(4096)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.read(4096)
    except Exception:
        return "error"

    if status == 201:
        return "created"
    if status == 200:
        return "duplicate"
    if 400 <= status < 500:
        return "rejected"
    return "error"


def main():
    endpoint = required_env("IOT_ENDPOINT")
    topic = required_env("IOT_TOPIC")
    client_id = required_env("IOT_CLIENT_ID")
    device_ids = {
        item.strip()
        for item in required_env("IOT_DEVICE_IDS").split(",")
        if item.strip()
    }
    ca_path = required_env("IOT_CA")
    cert_path = required_env("IOT_CERT")
    key_path = required_env("IOT_KEY")
    reporter_token = required_env("REPORTER_TOKEN")

    if not device_ids:
        raise ValueError("IOT_DEVICE_IDS must contain at least one device ID")

    for path in (ca_path, cert_path, key_path):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Required IoT file not found: {path}")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=ca_path,
        certfile=cert_path,
        keyfile=key_path,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    def on_connect(mqtt_client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("connect failed rc=%s", reason_code)
            return

        result, _mid = mqtt_client.subscribe(topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            LOG.error("subscribe request failed rc=%s", result)

    def on_subscribe(mqtt_client, userdata, mid, reason_code_list, properties):
        for reason_code in reason_code_list:
            LOG.info("subscribed rc=%s", reason_code)

    def on_message(mqtt_client, userdata, message):
        event_id = "unknown"
        result = "rejected"

        try:
            event = json.loads(message.payload.decode("utf-8"))
            if isinstance(event, dict):
                candidate = event.get("event_id")
                if isinstance(candidate, str) and candidate:
                    event_id = candidate

                device_id = event.get("device_id")
                if not isinstance(device_id, str) or device_id not in device_ids:
                    result = "rejected"
                else:
                    result = report_event(event, reporter_token)
            else:
                result = "rejected"
        except Exception:
            result = "rejected"

        # Log only event ID and outcome; never log tokens or message contents.
        LOG.info("event_id=%s result=%s", event_id, result)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    LOG.info("starting consumer")
    client.connect(endpoint, port=8883, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Print the exception type only; avoid accidentally exposing credentials.
        LOG.error("consumer startup failed: %s", type(exc).__name__)
        raise SystemExit(1)
