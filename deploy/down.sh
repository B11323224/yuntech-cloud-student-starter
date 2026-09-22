#!/usr/bin/env bash
set -euo pipefail

RESOURCE_FILE=".local/resources.json"

if [ ! -f "$RESOURCE_FILE" ]; then
  echo "錯誤：找不到 $RESOURCE_FILE，無法進行回收。"
  exit 1
fi

INSTANCE_ID=$(python3 -c "import json; print(json.load(open('$RESOURCE_FILE')).get('instance_id',''))")
SG_ID=$(python3 -c "import json; print(json.load(open('$RESOURCE_FILE')).get('security_group_id',''))")
KEY_NAME=$(python3 -c "import json; print(json.load(open('$RESOURCE_FILE')).get('key_name',''))")

# 支援 --stop 參數 (僅停止主機留待下週使用)
if [ "${1:-}" = "--stop" ]; then
  echo "=== [T4] 停止 EC2 主機 ($INSTANCE_ID) ==="
  if [ -n "$INSTANCE_ID" ]; then
    aws ec2 stop-instances --instance-ids "$INSTANCE_ID" > /dev/null
    aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
    echo "主機已成功停止 (stopped)。"
  fi
  exit 0
fi

echo "=== [T4] 清理與回收 AWS 資源 ==="

# 1. 終止並刪除 EC2 主機
if [ -n "$INSTANCE_ID" ]; then
  echo "正在終止 EC2 主機: $INSTANCE_ID ..."
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" > /dev/null || true
  aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" || true
  echo "EC2 主機已終止。"
fi

# 2. 刪除 Security Group
if [ -n "$SG_ID" ]; then
  echo "正在刪除 Security Group: $SG_ID ..."
  # 輪詢等待 SG 解除綁定
  MAX_RETRIES=15
  COUNT=0
  while [ $COUNT -lt $MAX_RETRIES ]; do
    if aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
      echo "Security Group 刪除成功。"
      break
    fi
    COUNT=$((COUNT + 1))
    sleep 5
  done
fi

# 3. 刪除 Key Pair
if [ -n "$KEY_NAME" ]; then
  echo "正在刪除 Key Pair: $KEY_NAME ..."
  aws ec2 delete-key-pair --key-name "$KEY_NAME" > /dev/null || true
  echo "Key Pair 刪除成功。"
fi

# 4. 清除資源檔內容
rm -f "$RESOURCE_FILE"
echo "=== 回收完成，所有資源已無殘留 ==="
