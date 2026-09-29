#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

COMMIT="${1:-HEAD}"
COMMIT_SHA="$(git rev-parse --verify --end-of-options "${COMMIT}^{commit}")"
SECRET_FILE=".local/app.env"
if [[ ! -f "$SECRET_FILE" || -L "$SECRET_FILE" ]]; then
  echo "STOP: .local/app.env is missing or is a symlink." >&2
  exit 1
fi
SECRET_MODE="$(stat -c '%a' "$SECRET_FILE")"
if [[ "$SECRET_MODE" != "600" ]]; then
  echo "STOP: .local/app.env must have mode 600." >&2
  exit 1
fi
python3 - "$SECRET_FILE" <<'PY'
from pathlib import Path
import sys

values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
  if not line or line.startswith("#"):
    continue
  key, separator, value = line.partition("=")
  if separator and key in {"REPORTER_TOKEN", "OPERATOR_TOKEN"}:
    values[key] = value
reporter = values.get("REPORTER_TOKEN", "")
operator = values.get("OPERATOR_TOKEN", "")
if not reporter or not operator or reporter == operator:
  raise SystemExit("STOP: app.env must contain two distinct non-empty tokens.")
PY

bash scripts/verify-aws.sh

CURRENT_EGRESS_IP="$(curl -4 --fail --silent --show-error --max-time 5 https://checkip.amazonaws.com | tr -d '\r\n')"
TARGET_INFO="$(CURRENT_EGRESS_IP="$CURRENT_EGRESS_IP" python3 deploy/target.py)"
INSTANCE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])' <<< "$TARGET_INFO")"
PUBLIC_IP="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["public_ip"])' <<< "$TARGET_INFO")"
KEY_PATH="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["key_path"])' <<< "$TARGET_INFO")"
REGION="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])' <<< "$TARGET_INFO")"
if [[ ! -f "$KEY_PATH" || -L "$KEY_PATH" || "$(stat -c '%a' "$KEY_PATH")" != "600" ]]; then
  echo "STOP: SSH private key must exist as a regular file with mode 600." >&2
  exit 1
fi

USER_DATA=".local/w04-user-data-${COMMIT_SHA:0:12}.sh"
if [[ -e "$USER_DATA" ]]; then
  echo "STOP: refusing to overwrite existing $USER_DATA." >&2
  exit 1
fi
bash deploy/make-user-data.sh "$COMMIT_SHA" "$USER_DATA"

printf 'W4 deployment plan\n  instance: %s\n  region: %s\n  public IPv4: %s\n  commit: %s\n  local secret file: %s (mode %s; contents not displayed)\n' \
  "$INSTANCE_ID" "$REGION" "$PUBLIC_IP" "$COMMIT_SHA" "$SECRET_FILE" "$SECRET_MODE"
printf 'This updates one existing host; no security-group, IAM, or other AWS resource changes.\n'
read -r -p 'After peer review, type DEPLOY W4 to continue: ' APPROVAL
if [[ "$APPROVAL" != "DEPLOY W4" ]]; then
  echo "Cancelled; no SSH deployment was attempted."
  exit 1
fi

SSH_OPTIONS=(-i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o ConnectTimeout=8)
SSH_TARGET="ec2-user@$PUBLIC_IP"
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" 'sudo bash -s' < "$USER_DATA"
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  'sudo sh -c '\''umask 077; mkdir -p /etc/inspection; cat > /etc/inspection/app.env; chown root:root /etc/inspection/app.env; chmod 600 /etc/inspection/app.env; systemctl restart inspection'\''' \
  < "$SECRET_FILE"

HEALTH="$(curl --fail --silent --show-error --max-time 8 "http://$PUBLIC_IP/health")"
HEALTH_JSON="$HEALTH" EXPECTED_COMMIT="$COMMIT_SHA" python3 - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH_JSON"])
if health.get("version") != os.environ["EXPECTED_COMMIT"] or health.get("auth_configured") is not True:
    raise SystemExit("STOP: /health version or auth_configured check failed")
print("W4 deployment verified: /health version matches commit and auth_configured is true.")
PY