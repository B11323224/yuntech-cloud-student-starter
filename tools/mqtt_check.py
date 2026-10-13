#!/usr/bin/env python3
import argparse
import json
import ssl
import sys
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT_DIR = ROOT / ".local" / "iot"
CODE = "group5-Qiao"
TOPIC = f"cxe/{CODE}/gw/events"


def main():
    parser = argparse.ArgumentParser(description="AWS IoT MQTT 連線測試")
    parser.add_argument("--as", dest="role", choices=["gw", "consumer"], required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pub", metavar="TOPIC")
    group.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=int, default=10)
    args = parser.parse_args()

    if args.wait < 1 or args.wait > 120:
        parser.error("--wait 必須介於 1 到 120 秒")

    if args.role == "gw" and not args.pub:
        parser.error("gw 必須使用 --pub")
    if args.role == "consumer" and not args.sub:
        parser.error("consumer 必須使用 --sub")

    if args.pub and args.pub != TOPIC:
        parser.error(f"本測試只允許指定 Topic：{TOPIC}")
    if args.sub and args.sub != TOPIC:
        parser.error(f"本測試只允許指定 Topic：{TOPIC}")

    prefix = "gw" if args.role == "gw" else "consumer"
    cert = IOT_DIR / f"{prefix}.cert.pem"
    key = IOT_DIR / f"{prefix}.private.key"
    ca = IOT_DIR / "AmazonRootCA1.pem"
    endpoint_file = IOT_DIR / "endpoint"

    for path in (cert, key, ca, endpoint_file):
        if not path.is_file() or path.stat().st_size == 0:
            sys.exit(f"ERROR: 憑證或設定檔不存在或為空：{path.name}")

    endpoint = endpoint_file.read_text(encoding="utf-8").strip()
    client_id = f"gw-{CODE}" if args.role == "gw" else f"consumer-{CODE}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=str(ca),
        certfile=str(cert),
        keyfile=str(key),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    state = {"connected": False, "connack": None, "suback": None,
             "received": False, "puback": None, "mid": None, "error": None}

    def on_connect(client, userdata, flags, reason_code, properties):
        state["connack"] = str(reason_code)
        print(f"connack={reason_code}")
        if reason_code.is_failure:
            state["error"] = f"連線失敗：{reason_code}"
            return
        state["connected"] = True
        if args.sub:
            client.subscribe(args.sub, qos=1)

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        codes = ",".join(str(code) for code in reason_code_list)
        state["suback"] = codes
        print(f"suback={codes}")
        if any(code.is_failure for code in reason_code_list):
            state["error"] = f"訂閱遭拒絕：{codes}"

    def on_message(client, userdata, msg):
        state["received"] = True
        print(f"recv topic={msg.topic} payload={msg.payload.decode('utf-8', errors='replace')}")

    def on_publish(client, userdata, mid, reason_code, properties):
        if mid == state["mid"]:
            state["puback"] = str(reason_code)
            print(f"puback={reason_code}")

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        if reason_code.is_failure:
            state["error"] = f"連線中斷：{reason_code}"

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_publish = on_publish
    client.on_disconnect = on_disconnect

    try:
        client.connect(endpoint, port=8883, keepalive=30)
        client.loop_start()
        deadline = time.monotonic() + args.wait

        while time.monotonic() < deadline and not state["connected"]:
            if state["error"]:
                break
            time.sleep(0.1)

        if not state["connected"]:
            raise RuntimeError(state["error"] or "連線逾時")

        if args.sub:
            while time.monotonic() < deadline and state["suback"] is None:
                if state["error"]:
                    break
                time.sleep(0.1)
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["suback"] is None:
                raise RuntimeError("等待 SUBACK 逾時")
            if not state["received"]:
                print(f"已訂閱，等待訊息最多 {args.wait} 秒")
                while time.monotonic() < deadline and not state["received"]:
                    time.sleep(0.1)
            if not state["received"]:
                print("尚未收到訊息；請確認 Gateway 是否已發布。")
                return 2
        else:
            payload = json.dumps({
                "check": "t2",
                "code": CODE,
                "event_id": f"mqtt-check-{uuid.uuid4()}",
            })
            info = client.publish(args.pub, payload=payload, qos=1)
            state["mid"] = info.mid
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"publish 呼叫失敗：{info.rc}")
            while time.monotonic() < deadline and state["puback"] is None:
                time.sleep(0.1)
            if state["puback"] is None:
                raise RuntimeError("等待 PUBACK 逾時")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        client.disconnect()
        client.loop_stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
