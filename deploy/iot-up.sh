#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

cd "$(git rev-parse --show-toplevel)"

readonly CODE="group5-Qiao"
readonly REGION="us-east-1"
readonly PROFILE="learnerlab"
readonly IOT_DIR=".local/iot"
readonly RESOURCES=".local/resources.json"

mkdir -p "$IOT_DIR"
chmod 700 "$IOT_DIR"

for cmd in aws python3 curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: 找不到必要指令：$cmd" >&2
        exit 1
    }
done

[[ -f "$RESOURCES" ]] || {
    echo "ERROR: 找不到 $RESOURCES" >&2
    exit 1
}

aws_cmd() {
    aws --profile "$PROFILE" --region "$REGION" "$@"
}

save_resource() {
    local key="$1" value="$2"
    RESOURCE_KEY="$key" RESOURCE_VALUE="$value" \
        python3 - "$RESOURCES" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
key = os.environ["RESOURCE_KEY"]
value = os.environ["RESOURCE_VALUE"]

if key in data and data[key] != value:
    raise SystemExit(f"ERROR: {key} 已存在且值不同，停止以避免覆蓋。")

data[key] = value
tmp = path.with_suffix(".json.tmp")
tmp.write_text(
    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
tmp.replace(path)
PY
}

echo "[1/6] 檢查 AWS 身分與區域"
ACCOUNT_ID="$(aws_cmd sts get-caller-identity --query Account --output text)"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
    echo "ERROR: 無法取得有效 AWS Account ID" >&2
    exit 1
}
echo "AWS 身分確認完成；區域：$REGION"

echo "[2/6] 取得 IoT endpoint 與 Root CA"
ENDPOINT="$(aws_cmd iot describe-endpoint \
    --endpoint-type iot:Data-ATS \
    --query endpointAddress --output text)"

[[ "$ENDPOINT" == *.amazonaws.com ]] || {
    echo "ERROR: IoT endpoint 格式不正確" >&2
    exit 1
}

if [[ -f "$IOT_DIR/endpoint" ]]; then
    OLD_ENDPOINT="$(cat "$IOT_DIR/endpoint")"
    [[ "$OLD_ENDPOINT" == "$ENDPOINT" ]] || {
        echo "ERROR: 已存在不同的 endpoint，請先人工檢查。" >&2
        exit 1
    }
else
    printf '%s\n' "$ENDPOINT" > "$IOT_DIR/endpoint"
fi

if [[ ! -f "$IOT_DIR/AmazonRootCA1.pem" ]]; then
    curl --fail --silent --show-error --location \
        https://www.amazontrust.com/repository/AmazonRootCA1.pem \
        --output "$IOT_DIR/AmazonRootCA1.pem"
fi

chmod 600 "$IOT_DIR/endpoint" "$IOT_DIR/AmazonRootCA1.pem"

create_or_reuse_cert() {
    local role="$1"
    local resource_key="$2"
    local cert_file="$IOT_DIR/$role.cert.pem"
    local key_file="$IOT_DIR/$role.private.key"
    local id=""
    local pub_tmp="$IOT_DIR/.$role.public.tmp"

    id="$(python3 - "$RESOURCES" "$resource_key" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], ""))
PY
)"

    if [[ -n "$id" ]]; then
        [[ -s "$cert_file" && -s "$key_file" ]] || {
            echo "ERROR: $role 憑證 ID 已存在，但憑證或私密金鑰檔案缺失；不重新建立。" >&2
            exit 1
        }
        echo "$role 憑證已存在，沿用原有憑證。"
    else
        [[ ! -e "$cert_file" && ! -e "$key_file" ]] || {
            echo "ERROR: 發現未登記的 $role 憑證檔案；不覆蓋、不重建。" >&2
            exit 1
        }

        echo "建立 $role 憑證（私密金鑰直接寫入檔案）"
        id="$(aws_cmd iot create-keys-and-certificate \
            --set-as-active \
            --certificate-pem-outfile "$cert_file" \
            --private-key-outfile "$key_file" \
            --public-key-outfile "$pub_tmp" \
            --query certificateId --output text)"
        rm -f "$pub_tmp"

        [[ "$id" =~ ^[0-9a-fA-F-]+$ ]] || {
            echo "ERROR: 未取得有效的 $role 憑證 ID" >&2
            exit 1
        }

        chmod 600 "$cert_file" "$key_file"
        save_resource "$resource_key" "$id"
    fi

    local status
    status="$(aws_cmd iot describe-certificate \
        --certificate-id "$id" \
        --query 'certificateDescription.status' --output text)"

    if [[ "$status" != "ACTIVE" ]]; then
        aws_cmd iot update-certificate \
            --certificate-id "$id" --new-status ACTIVE
    fi

    status="$(aws_cmd iot describe-certificate \
        --certificate-id "$id" \
        --query 'certificateDescription.status' --output text)"

    [[ "$status" == "ACTIVE" ]] || {
        echo "ERROR: $role 憑證不是 ACTIVE" >&2
        exit 1
    }
}

echo "[3/6] 建立或沿用兩組憑證"
create_or_reuse_cert gw iot_gw_cert_id
create_or_reuse_cert consumer iot_consumer_cert_id

echo "[4/6] 建立最小權限 Policies"
GW_POLICY="cxe-${CODE}-gw"
CONSUMER_POLICY="cxe-${CODE}-consumer"

GW_DOC="$IOT_DIR/.gw-policy.json"
CONSUMER_DOC="$IOT_DIR/.consumer-policy.json"

ACCOUNT_ID="$ACCOUNT_ID" CODE="$CODE" python3 - <<'PY'
import json, os
from pathlib import Path

d = Path(".local/iot")
account = os.environ["ACCOUNT_ID"]
code = os.environ["CODE"]
base = f"arn:aws:iot:us-east-1:{account}:"

docs = {
    ".gw-policy.json": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": base + f"client/gw-{code}",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": base + f"topic/cxe/{code}/gw/events",
            },
        ],
    },
    ".consumer-policy.json": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": base + f"client/consumer-{code}",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Subscribe",
                "Resource": base + f"topicfilter/cxe/{code}/gw/events",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Receive",
                "Resource": base + f"topic/cxe/{code}/gw/events",
            },
        ],
    },
}
for name, doc in docs.items():
    (d / name).write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
PY
chmod 600 "$GW_DOC" "$CONSUMER_DOC"

ensure_policy() {
    local name="$1" doc="$2"
    if aws_cmd iot get-policy --policy-name "$name" >/dev/null 2>&1; then
        echo "Policy 已存在，沿用：$name"
    else
        aws_cmd iot create-policy \
            --policy-name "$name" \
            --policy-document "file://$doc" >/dev/null
        echo "Policy 已建立：$name"
    fi
}

ensure_policy "$GW_POLICY" "$GW_DOC"
ensure_policy "$CONSUMER_POLICY" "$CONSUMER_DOC"

GW_ID="$(python3 -c \
    'import json; print(json.load(open(".local/resources.json"))["iot_gw_cert_id"])')"
CONSUMER_ID="$(python3 -c \
    'import json; print(json.load(open(".local/resources.json"))["iot_consumer_cert_id"])')"

aws_cmd iot attach-policy --policy-name "$GW_POLICY" \
    --target "arn:aws:iot:$REGION:$ACCOUNT_ID:cert/$GW_ID"
aws_cmd iot attach-policy --policy-name "$CONSUMER_POLICY" \
    --target "arn:aws:iot:$REGION:$ACCOUNT_ID:cert/$CONSUMER_ID"

save_resource iot_gw_policy "$GW_POLICY"
save_resource iot_consumer_policy "$CONSUMER_POLICY"

echo "[5/6] 檢查檔案權限"
chmod 700 "$IOT_DIR"
chmod 600 "$IOT_DIR/endpoint" \
    "$IOT_DIR/AmazonRootCA1.pem" \
    "$IOT_DIR/gw.cert.pem" \
    "$IOT_DIR/gw.private.key" \
    "$IOT_DIR/consumer.cert.pem" \
    "$IOT_DIR/consumer.private.key"

echo "[6/6] 完成"
echo "Endpoint：$ENDPOINT"
echo "Gateway 與 Consumer 憑證均為 ACTIVE。"
echo "私密金鑰未輸出到終端機。"
echo "請接著執行 MQTT 連線測試。"
