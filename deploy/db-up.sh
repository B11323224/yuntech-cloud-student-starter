#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-us-east-1}"
VPC_ID="vpc-05feacce6e0dda6f6"
HOST_SG_ID="sg-0962fe3c99d2edb99"

SUBNET_A_CIDR="172.31.96.0/24"
SUBNET_B_CIDR="172.31.97.0/24"
AZ_A="us-east-1a"
AZ_B="us-east-1b"

DB_IDENTIFIER="inspection"
DB_NAME="inspection"
DB_USER="inspection_admin"
DB_SUBNET_GROUP="inspection-private-subnet-group"
DB_SG_NAME="inspection-db-sg"

LOCAL_DIR=".local"
RESOURCES_FILE="$LOCAL_DIR/resources.json"
DB_ENV="$LOCAL_DIR/db.env"

mkdir -p "$LOCAL_DIR"

if [[ -e "$DB_ENV" || -L "$DB_ENV" ]]; then
  echo "STOP: $DB_ENV already exists or is a symlink." >&2
  exit 1
fi

write_resource() {
  local key="$1"
  local value="$2"

  python3 - "$RESOURCES_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
data[key] = value
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
}

echo "Checking VPC..."
VPC_CIDR="$(aws ec2 describe-vpcs \
  --region "$REGION" \
  --vpc-ids "$VPC_ID" \
  --query 'Vpcs[0].CidrBlock' \
  --output text)"

if [[ "$VPC_CIDR" != "172.31.0.0/16" ]]; then
  echo "STOP: unexpected VPC CIDR: $VPC_CIDR" >&2
  exit 1
fi

echo "Creating private subnet A..."
SUBNET_A_ID="$(aws ec2 create-subnet \
  --region "$REGION" \
  --vpc-id "$VPC_ID" \
  --cidr-block "$SUBNET_A_CIDR" \
  --availability-zone "$AZ_A" \
  --query 'Subnet.SubnetId' \
  --output text)"

write_resource "w05_private_subnet_a_id" "$SUBNET_A_ID"

echo "Creating private subnet B..."
SUBNET_B_ID="$(aws ec2 create-subnet \
  --region "$REGION" \
  --vpc-id "$VPC_ID" \
  --cidr-block "$SUBNET_B_CIDR" \
  --availability-zone "$AZ_B" \
  --query 'Subnet.SubnetId' \
  --output text)"

write_resource "w05_private_subnet_b_id" "$SUBNET_B_ID"

echo "Creating dedicated route table..."
RT_ID="$(aws ec2 create-route-table \
  --region "$REGION" \
  --vpc-id "$VPC_ID" \
  --query 'RouteTable.RouteTableId' \
  --output text)"

write_resource "w05_private_route_table_id" "$RT_ID"

aws ec2 associate-route-table \
  --region "$REGION" \
  --subnet-id "$SUBNET_A_ID" \
  --route-table-id "$RT_ID" >/dev/null

aws ec2 associate-route-table \
  --region "$REGION" \
  --subnet-id "$SUBNET_B_ID" \
  --route-table-id "$RT_ID" >/dev/null

echo "Creating DB security group..."
DB_SG_ID="$(aws ec2 create-security-group \
  --region "$REGION" \
  --group-name "$DB_SG_NAME" \
  --description "Inspection RDS PostgreSQL private SG" \
  --vpc-id "$VPC_ID" \
  --query 'GroupId' \
  --output text)"

write_resource "w05_db_security_group_id" "$DB_SG_ID"

aws ec2 authorize-security-group-ingress \
  --region "$REGION" \
  --group-id "$DB_SG_ID" \
  --protocol tcp \
  --port 5432 \
  --source-group "$HOST_SG_ID" >/dev/null

echo "Creating DB subnet group..."
aws rds create-db-subnet-group \
  --region "$REGION" \
  --db-subnet-group-name "$DB_SUBNET_GROUP" \
  --db-subnet-group-description "Private subnets for inspection RDS" \
  --subnet-ids "$SUBNET_A_ID" "$SUBNET_B_ID" >/dev/null

write_resource "w05_db_subnet_group" "$DB_SUBNET_GROUP"

echo "Generating DB password..."
DB_PASSWORD="$(python3 - <<'PY'
import secrets
import string

alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(24)))
PY
)"

umask 077
cat > "$DB_ENV" <<EOF_ENV
DB_HOST=
DB_PORT=5432
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
EOF_ENV
chmod 600 "$DB_ENV"

echo "Creating RDS PostgreSQL instance..."

RDS_INPUT="$(mktemp "$LOCAL_DIR/rds-input.XXXXXX.json")"
chmod 600 "$RDS_INPUT"
trap 'rm -f "$RDS_INPUT"' EXIT

python3 - "$RDS_INPUT" <<'PY_RDS'
import json
import sys

from pathlib import Path

output = Path(sys.argv[1])

data = {
    "DBInstanceIdentifier": "inspection",
    "DBInstanceClass": "db.t3.micro",
    "Engine": "postgres",
    "AllocatedStorage": 20,
    "StorageType": "gp3",
    "StorageEncrypted": True,
    "PubliclyAccessible": False,
    "AvailabilityZone": "us-east-1a",
    "DBSubnetGroupName": "inspection-private-subnet-group",
    "VpcSecurityGroupIds": ["$DB_SG_ID"],
    "MasterUsername": "$DB_USER",
    "MasterUserPassword": "$DB_PASSWORD",
    "DBName": "inspection",
    "BackupRetentionPeriod": 0,
    "AutoMinorVersionUpgrade": False
}

output.write_text(json.dumps(data), encoding="utf-8")
PY_RDS

aws rds create-db-instance \
  --region "$REGION" \
  --cli-input-json "file://$RDS_INPUT" \
  >/dev/null

rm -f "$RDS_INPUT"
trap - EXIT

write_resource "w05_db_instance_identifier" "$DB_IDENTIFIER"

echo
echo "RDS creation started."
echo "Do NOT rerun deploy/db-up.sh while the database is being created."
echo "Waiting for RDS to become available..."

aws rds wait db-instance-available \
  --region "$REGION" \
  --db-instance-identifier "$DB_IDENTIFIER"

DB_INFO="$(aws rds describe-db-instances \
  --region "$REGION" \
  --db-instance-identifier "$DB_IDENTIFIER" \
  --query 'DBInstances[0].{Status:DBInstanceStatus,PubliclyAccessible:PubliclyAccessible,Endpoint:Endpoint.Address,Port:Endpoint.Port,Arn:DBInstanceArn}' \
  --output json)"

DB_STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Status"])' <<< "$DB_INFO")"
DB_PUBLIC="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["PubliclyAccessible"])' <<< "$DB_INFO")"
DB_HOST="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Endpoint"])' <<< "$DB_INFO")"
DB_PORT_VALUE="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["Port"])' <<< "$DB_INFO")"

if [[ "$DB_STATUS" != "available" ]]; then
  echo "STOP: RDS status is not available: $DB_STATUS" >&2
  exit 1
fi

if [[ "$DB_PUBLIC" != "False" ]]; then
  echo "STOP: RDS is unexpectedly public: $DB_PUBLIC" >&2
  exit 1
fi

python3 - "$DB_ENV" "$DB_HOST" "$DB_PORT_VALUE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
host = sys.argv[2]
port = sys.argv[3]

lines = path.read_text(encoding="utf-8").splitlines()

for i, line in enumerate(lines):
    if line.startswith("DB_HOST="):
        lines[i] = "DB_HOST=" + host
    elif line.startswith("DB_PORT="):
        lines[i] = "DB_PORT=" + port

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

chmod 600 "$DB_ENV"

write_resource "w05_db_status" "$DB_STATUS"
write_resource "w05_db_publicly_accessible" "$DB_PUBLIC"
write_resource "w05_db_endpoint" "$DB_HOST"
write_resource "w05_db_port" "$DB_PORT_VALUE"

echo
echo "RDS readback:"
printf '  status: %s\n' "$DB_STATUS"
printf '  PubliclyAccessible: %s\n' "$DB_PUBLIC"
printf '  endpoint: %s\n' "$DB_HOST"
printf '  port: %s\n' "$DB_PORT_VALUE"
printf '  db secret: %s (mode %s; password not displayed)\n' \
  "$DB_ENV" "$(stat -c '%a' "$DB_ENV")"

echo
echo "W5 database setup completed."
