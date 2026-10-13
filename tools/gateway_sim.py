#!/usr/bin/env python3
import argparse
import json
import re
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", args.code):
        parser.error("--code 格式錯誤")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", args.run):
        parser.error("--run 格式錯誤")

    prefix = f"{args.code}-{args.run}"
    topic = f"cxe/{args.code}/gw/events"
    paths = [
        IOT / "gw.cert.pem",
        IOT / "gw.private.key",
        IOT / "AmazonRootCA1.pem",
        IOT / "endpoint",
    ]
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            sys.exit(f"ERROR: 檔案不存在或為空：{path.name}")

    endpoint = (IOT / "endpoint").read_text().strip()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"gw-{args.code}",
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=str(IOT / "AmazonRootCA1.pem"),
        certfile=str(IOT / "gw.cert.pem"),
        keyfile=str(IOT / "gw.private.key"),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    state = {"connected": False, "error": None, "pubacks": {}}

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connack={reason_code}")
        if reason_code.is_failure:
            state["error"] = f"連線失敗：{reason_code}"
        else:
            state["connected"] = True

    def on_publish(client, userdata, mid, reason_code, properties):
        state["pubacks"][mid] = str(reason_code)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            state["error"] = f"連線中斷：{reason_code}"

    client.on_connect = on_connect
    client.on_publish = on_publish
    client.on_disconnect = on_disconnect

    try:
        client.connect(endpoint, 8883, keepalive=30)
        client.loop_start()
        deadline = time.monotonic() + 15
        while not state["connected"] and time.monotonic() < deadline:
            if state["error"]:
                raise RuntimeError(state["error"])
            time.sleep(0.1)
        if not state["connected"]:
            raise RuntimeError("等待 MQTT 連線逾時")

        events = []
        first = None
        for device_no in range(1, 11):
            for seq in range(1, 4):
                event = {
                    "event_id": f"{prefix}-d{device_no:02d}-{seq:02d}",
                    "device_id": f"{args.code}-d{device_no:02d}",
                    "observed_at": datetime.now(
                        ZoneInfo("Asia/Taipei")
                    ).isoformat(),
                    "type": "status",
                }
                if first is None:
                    first = event.copy()
                events.append(event)

        events.append(first.copy())
        events.append({
            "event_id": f"{prefix}-d99-01",
            "device_id": f"{args.code}-d99",
            "observed_at": datetime.now(
                ZoneInfo("Asia/Taipei")
            ).isoformat(),
            "type": "status",
        })

        ok = 0
        for event in events:
            info = client.publish(
                topic, json.dumps(event, ensure_ascii=False), qos=1
            )
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"發布失敗：{info.rc}")

            deadline = time.monotonic() + 15
            while info.mid not in state["pubacks"] and time.monotonic() < deadline:
                if state["error"]:
                    raise RuntimeError(state["error"])
                time.sleep(0.05)

            if info.mid not in state["pubacks"]:
                raise RuntimeError(f"等待 PUBACK 逾時，event_id={event['event_id']}")
            reason = state["pubacks"].pop(info.mid)
            if reason != "Success":
                raise RuntimeError(f"PUBACK 非成功：{reason}")
            ok += 1

        result = {"prefix": prefix, "puback_ok": ok}
        output = ROOT / ".local" / "w06-last-run.json"
        output.write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        output.chmod(0o600)
        print(f"prefix={prefix}")
        print(f"puback_ok={ok}/32")
        return 0 if ok == 32 else 1

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
