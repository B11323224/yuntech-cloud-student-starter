#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

COMMIT="${1:-HEAD}"
COMMIT_SHA="$(git rev-parse --verify --end-of-options "${COMMIT}^{commit}")"

APP_SECRET_FILE=".local/app.env"
DB_SECRET_FILE=".local/db.env"

for SECRET_FILE in "$APP_SECRET_FILE" "$DB_SECRET_FILE"; do
  if [[ ! -f "$SECRET_FILE" || -L "$SECRET_FILE" ]]; then
    echo "STOP: $SECRET_FILE is missing or is a symlink." >&2
    exit 1
  fi

  SECRET_MODE="$(stat -c '%a' "$SECRET_FILE")"
  if [[ "$SECRET_MODE" != "600" ]]; then
    echo "STOP: $SECRET_FILE must have mode 600." >&2
    exit 1
  fi
done

python3 - "$APP_SECRET_FILE" "$DB_SECRET_FILE" <<'PY'
from pathlib import Path
import sys

app_values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    key, separator, value = line.partition("=")
    if separator and key in {"REPORTER_TOKEN", "OPERATOR_TOKEN"}:
        app_values[key] = value

reporter = app_values.get("REPORTER_TOKEN", "")
operator = app_values.get("OPERATOR_TOKEN", "")

if not reporter or not operator or reporter == operator:
    raise SystemExit(
        "STOP: app.env must contain two distinct non-empty tokens."
    )

db_values = {}
for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#"):
        continue
    key, separator, value = line.partition("=")
    if separator and key in {
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    }:
        db_values[key] = value

required = {
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
}

if not required.issubset(db_values):
    raise SystemExit(
        "STOP: db.env is missing one or more required DB settings."
    )

if any(not db_values[key] for key in required):
    raise SystemExit(
        "STOP: db.env contains an empty required DB setting."
    )

if db_values["DB_PORT"] != "5432":
    raise SystemExit("STOP: DB_PORT must be 5432.")
PY

bash scripts/verify-aws.sh

CURRENT_EGRESS_IP="$(
  curl -4 --fail --silent --show-error --max-time 5 \
    https://checkip.amazonaws.com | tr -d '\r\n'
)"

TARGET_INFO="$(
  CURRENT_EGRESS_IP="$CURRENT_EGRESS_IP" python3 deploy/target.py
)"

INSTANCE_ID="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])' \
    <<< "$TARGET_INFO"
)"

PUBLIC_IP="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin)["public_ip"])' \
    <<< "$TARGET_INFO"
)"

KEY_PATH="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin)["key_path"])' \
    <<< "$TARGET_INFO"
)"

REGION="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])' \
    <<< "$TARGET_INFO"
)"

if [[ ! -f "$KEY_PATH" || -L "$KEY_PATH" || "$(stat -c '%a' "$KEY_PATH")" != "600" ]]; then
  echo "STOP: SSH private key must exist as a regular file with mode 600." >&2
  exit 1
fi

USER_DATA=".local/w05-user-data-${COMMIT_SHA:0:12}.sh"

if [[ -e "$USER_DATA" ]]; then
  echo "STOP: refusing to overwrite existing $USER_DATA." >&2
  exit 1
fi

bash deploy/make-user-data.sh "$COMMIT_SHA" "$USER_DATA"

printf 'W5 deployment plan\n'
printf '  instance: %s\n' "$INSTANCE_ID"
printf '  region: %s\n' "$REGION"
printf '  public IPv4: %s\n' "$PUBLIC_IP"
printf '  commit: %s\n' "$COMMIT_SHA"
printf '  app secret: %s (mode 600; contents not displayed)\n' "$APP_SECRET_FILE"
printf '  DB secret: %s (mode 600; contents not displayed)\n' "$DB_SECRET_FILE"
printf 'This updates one existing host; no security-group, IAM, or other AWS resource changes.\n'

read -r -p 'After peer review, type DEPLOY W5 to continue: ' APPROVAL

if [[ "$APPROVAL" != "DEPLOY W5" ]]; then
  echo "Cancelled; no SSH deployment was attempted."
  exit 1
fi

SSH_OPTIONS=(
  -i "$KEY_PATH"
  -o StrictHostKeyChecking=accept-new
  -o IdentitiesOnly=yes
  -o ConnectTimeout=8
)

SSH_TARGET="ec2-user@$PUBLIC_IP"

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" 'sudo bash -s' < "$USER_DATA"

TEMP_REMOTE_ENV="$(mktemp)"
trap 'rm -f "$TEMP_REMOTE_ENV"' EXIT
chmod 600 "$TEMP_REMOTE_ENV"

cat "$APP_SECRET_FILE" > "$TEMP_REMOTE_ENV"
printf '\n' >> "$TEMP_REMOTE_ENV"
cat "$DB_SECRET_FILE" >> "$TEMP_REMOTE_ENV"

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
  'sudo sh -c '"'"'umask 077; mkdir -p /etc/inspection; cat > /etc/inspection/app.env; chown root:root /etc/inspection/app.env; chmod 600 /etc/inspection/app.env; systemctl restart inspection'"'"'' \
  < "$TEMP_REMOTE_ENV"

HEALTH="$(
  curl --fail --silent --show-error --max-time 8 \
    "http://$PUBLIC_IP/health"
)"

HEALTH_JSON="$HEALTH" EXPECTED_COMMIT="$COMMIT_SHA" python3 - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH_JSON"])

if health.get("version") != os.environ["EXPECTED_COMMIT"]:
    raise SystemExit(
        "STOP: /health version does not match deployed commit."
    )

if health.get("auth_configured") is not True:
    raise SystemExit(
        "STOP: /health auth_configured is not true."
    )

if health.get("db_configured") is not True:
    raise SystemExit(
        "STOP: /health db_configured is not true."
    )

print(
    "W5 deployment verified: version, auth_configured "
    "and db_configured are correct."
)
PY
