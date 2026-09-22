#!/usr/bin/env bash
set -euo pipefail

mkdir -p .local ~/.ssh
RESOURCE_FILE=".local/resources.json"
COMMIT_SHA=$(git rev-parse HEAD)
OWNER="Qiao"
GROUP="group5"

TAG_ARG="[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER}]"

echo "=== [T2 Step 1] 打包 User Data ==="
bash deploy/make-user-data.sh HEAD .local/w03-user-data.sh

echo "=== [T2 Step 2] 取得環境資訊 ==="
CODESPACE_IP="$(curl -s -4 ifconfig.me)/32"
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text)
SUBNET_ID=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" --query "Subnets[0].SubnetId" --output text)
AMI_ID=$(aws ec2 describe-images --owners amazon --filters "Name=name,Values=al2023-ami-2023*-x86_64" "Name=state,Values=available" --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)

echo "{\"vpc_id\":\"$VPC_ID\",\"subnet_id\":\"$SUBNET_ID\",\"ami_id\":\"$AMI_ID\"}" > "$RESOURCE_FILE"

echo "=== [T2 Step 3] 建立 Security Group ==="
SG_NAME="secgrp-w03-$OWNER-$(date +%s)"
SG_ID=$(aws ec2 create-security-group \
  --group-name "$SG_NAME" \
  --description "W3 SG for $OWNER" \
  --vpc-id "$VPC_ID" \
  --tag-specifications "ResourceType=security-group,Tags=$TAG_ARG" \
  --query "GroupId" --output text)

aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$CODESPACE_IP"
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 80 --cidr "$CODESPACE_IP"

python3 -c "import json; d=json.load(open('$RESOURCE_FILE')); d['security_group_id']='$SG_ID'; json.dump(d, open('$RESOURCE_FILE','w'))"

echo "=== [T2 Step 4] 匯入 Key Pair ==="
KEY_NAME="key-w03-$OWNER"
SSH_KEY_PATH="$HOME/.ssh/id_ed25519"
if [ ! -f "$SSH_KEY_PATH" ]; then
  ssh-keygen -t ed25519 -N "" -f "$SSH_KEY_PATH"
fi
chmod 600 "$SSH_KEY_PATH"

aws ec2 import-key-pair \
  --key-name "$KEY_NAME" \
  --public-key-material fileb://"$SSH_KEY_PATH.pub" \
  --tag-specifications "ResourceType=key-pair,Tags=$TAG_ARG" > /dev/null || true

python3 -c "import json; d=json.load(open('$RESOURCE_FILE')); d['key_name']='$KEY_NAME'; json.dump(d, open('$RESOURCE_FILE','w'))"

echo "=== [T2 Step 5] 啟動 EC2 主機 ==="
INSTANCE_TAGS="[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=$GROUP},{Key=owner,Value=$OWNER},{Key=Name,Value=w03-inspection-$OWNER}]"

INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type t3.micro \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SG_ID" \
  --subnet-id "$SUBNET_ID" \
  --user-data file://.local/w03-user-data.sh \
  --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":8,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":true}}]" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --tag-specifications "ResourceType=instance,Tags=$INSTANCE_TAGS" \
  --query "Instances[0].InstanceId" --output text)

python3 -c "import json; d=json.load(open('$RESOURCE_FILE')); d['instance_id']='$INSTANCE_ID'; json.dump(d, open('$RESOURCE_FILE','w'))"

echo "等待 EC2 啟動並取得 Public IP..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
python3 -c "import json; d=json.load(open('$RESOURCE_FILE')); d['public_ip']='$PUBLIC_IP'; json.dump(d, open('$RESOURCE_FILE','w'))"

echo "=== [T2 Step 6] 輪詢驗證 /health ==="
echo "EC2 Public IP: $PUBLIC_IP"
MAX_RETRIES=30
COUNT=0
HEALTH_OK=false

while [ $COUNT -lt $MAX_RETRIES ]; do
  RESPONSE=$(curl -s --max-time 3 "http://$PUBLIC_IP/health" || true)
  if echo "$RESPONSE" | grep -q "$COMMIT_SHA"; then
    HEALTH_OK=true
    echo "驗證成功！/health 回應內容: $RESPONSE"
    break
  fi
  COUNT=$((COUNT + 1))
  sleep 5
done

if [ "$HEALTH_OK" = false ]; then
  echo "驗證尚未就緒，資源已寫入 $RESOURCE_FILE"
  exit 1
fi

echo "=== 部署全數完成！ ==="
