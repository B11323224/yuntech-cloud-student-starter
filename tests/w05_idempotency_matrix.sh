#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://52.54.195.141"
EC2_HOST="52.54.195.141"
SSH_KEY="/home/vscode/.ssh/id_ed25519"
EVENT_ID="w5-demo-003"
DEVICE_ID="w5-device01"
OBSERVED_AT="2026-10-06T10:00:00+08:00"

set -a
source .local/app.env
set +a

echo "=== W5 Idempotency Matrix ==="

echo "--- health ---"
curl -sS "$BASE_URL/health"
echo

echo "--- #1 new event ---"
BODY_1=$(mktemp)
STATUS_1=$(curl -sS -o "$BODY_1" -w "%{http_code}" \
  -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_id\": \"$EVENT_ID\",
    \"device_id\": \"$DEVICE_ID\",
    \"observed_at\": \"$OBSERVED_AT\",
    \"type\": \"test\",
    \"note\": \"W5 matrix test\"
  }")
echo "status=$STATUS_1 body=$(cat "$BODY_1")"
rm -f "$BODY_1"

echo "--- #2 exact resend ---"
BODY_2=$(mktemp)
STATUS_2=$(curl -sS -o "$BODY_2" -w "%{http_code}" \
  -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_id\": \"$EVENT_ID\",
    \"device_id\": \"$DEVICE_ID\",
    \"observed_at\": \"$OBSERVED_AT\",
    \"type\": \"test\",
    \"note\": \"W5 matrix test\"
  }")
echo "status=$STATUS_2 body=$(cat "$BODY_2")"
rm -f "$BODY_2"

echo "--- #3 same ID different note ---"
BODY_3=$(mktemp)
STATUS_3=$(curl -sS -o "$BODY_3" -w "%{http_code}" \
  -X POST "$BASE_URL/events" \
  -H "Authorization: Bearer $REPORTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_id\": \"$EVENT_ID\",
    \"device_id\": \"$DEVICE_ID\",
    \"observed_at\": \"$OBSERVED_AT\",
    \"type\": \"test\",
    \"note\": \"W5 matrix DIFFERENT note\"
  }")
echo "status=$STATUS_3 body=$(cat "$BODY_3")"
rm -f "$BODY_3"

echo "--- #4 restart then check #1 ---"
ssh -i "$SSH_KEY" "ec2-user@$EC2_HOST" \
  "sudo systemctl restart inspection && sleep 3 && sudo systemctl is-active inspection"

BODY_4=$(mktemp)
STATUS_4=$(curl -sS -o "$BODY_4" -w "%{http_code}" \
  "$BASE_URL/events/$EVENT_ID" \
  -H "Authorization: Bearer $OPERATOR_TOKEN")
echo "status=$STATUS_4 body=$(cat "$BODY_4")"
rm -f "$BODY_4"

echo "--- #5 EC2 psql count ---"

ssh -i "$SSH_KEY" "ec2-user@$EC2_HOST" \
"sudo bash -c 'set -a; . /etc/inspection/app.env; set +a
PGPASSWORD=\"\$DB_PASSWORD\" psql \"host=\$DB_HOST dbname=\$DB_NAME user=\$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem\" \
  -v event_id=\"$EVENT_ID\"' <<'SQL'
SELECT count(*) FROM events WHERE event_id = :'event_id';
SQL"
