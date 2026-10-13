#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def request_json(url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


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
    run_file = ROOT / ".local" / "w06-last-run.json"
    app_file = ROOT / ".local" / "app.env"
    if not run_file.is_file() or not app_file.is_file():
        sys.exit("STOP: 缺少執行紀錄或 app.env")

    run_data = json.loads(run_file.read_text())
    if run_data.get("prefix") != prefix:
        sys.exit("STOP: 本次前綴與 w06-last-run.json 不一致")
    if run_data.get("puback_ok") != 32:
        sys.exit("STOP: PUBACK 數不是 32")

    app_env = read_env(app_file)
    token = app_env.get("OPERATOR_TOKEN", "")
    if not token:
        sys.exit("STOP: OPERATOR_TOKEN 未設定")

    current_ip = subprocess.run(
        ["curl", "-4", "--fail", "--silent", "--show-error",
         "--max-time", "5", "https://checkip.amazonaws.com"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    target_env = os.environ.copy()
    target_env["CURRENT_EGRESS_IP"] = current_ip
    target = json.loads(subprocess.run(
        ["python3", "deploy/target.py"],
        cwd=ROOT, env=target_env, check=True,
        capture_output=True, text=True,
    ).stdout)

    host = target["public_ip"]
    key = target["key_path"]
    if not Path(key).is_file() or Path(key).is_symlink():
        sys.exit("STOP: SSH 金鑰不存在或不是一般檔案")
    if oct(Path(key).stat().st_mode & 0o777) != "0o600":
        sys.exit("STOP: SSH 金鑰權限必須是 600")

    base_url = f"http://{host}"
    health = request_json(base_url + "/health")
    print(f"version={health.get('version')}")
    print(f"db_configured={health.get('db_configured')}")
    print(f"prefix={prefix}")
    print(f"puback_ok={run_data['puback_ok']}/32")

    remote_script = f"""#!/usr/bin/env bash
set -e
PREFIX={prefix!r}
for attempt in $(seq 1 12); do
  counts="$(sudo journalctl -u inspection-consumer -o cat --no-pager | grep -F "event_id=${{PREFIX}}-" | grep -o 'result=[a-z]*' | sort | uniq -c || true)"
  db_count="$(sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" -v prefix="$1" -At' _ "${{PREFIX}}-" <<'SQL'
SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
SQL
)"
  echo "=== consumer counts (attempt $attempt) ==="
  printf '%s\\n' "$counts"
  echo "db_prefix_count=$db_count"
  total="$(printf '%s\\n' "$counts" | awk '{{s += $1}} END {{print s+0}}')"
  if [ "$total" -ge 32 ] && [ "$db_count" = "30" ]; then
    break
  fi
  if [ "$attempt" -lt 12 ]; then sleep 5; fi
done
"""
    ssh = [
        "ssh", "-i", key, "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10", f"ec2-user@{host}", "bash -s",
    ]
    result = subprocess.run(
        ssh, input=remote_script, text=True,
        capture_output=True, timeout=90,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        sys.exit("STOP: SSH 對帳失敗")
    print(result.stdout, end="")

    device = f"{args.code}-d01"
    filtered = request_json(
        base_url + "/events?device_id=" + device, token
    )
    items = filtered if isinstance(filtered, list) else filtered.get("events", [])
    matching = [
        item.get("event_id", "")
        for item in items
        if item.get("event_id", "").startswith(prefix + "-")
    ]
    print(f"device_id={device}")
    for event_id in matching:
        print(f"event_id={event_id}")
    print(f"device_prefix_count={len(matching)}")
    if len(matching) != 3:
        sys.exit("STOP: device prefix count is not 3")


if __name__ == "__main__":
    main()
