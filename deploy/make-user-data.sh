#!/usr/bin/env bash
set -euo pipefail

COMMIT_REF="${1:-HEAD}"
OUTPUT_FILE="${2:-.local/w03-user-data.sh}"

COMMIT_SHA=$(git rev-parse "$COMMIT_REF")

mkdir -p "$(dirname "$OUTPUT_FILE")"

cat << USERDATA > "$OUTPUT_FILE"
#!/usr/bin/env bash
set -euo pipefail

# 更新套件並安裝 Nginx 與 Python3
dnf update -y
dnf install -y nginx python3

# 寫入 Inspection 服務檔案
mkdir -p /opt/inspection
cat << 'APP' > /opt/inspection/app.py
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            res = {
                "status": "ok",
                "service": "inspection",
                "version": "$COMMIT_SHA",
                "started_at": datetime.now(timezone.utc).isoformat()
            }
            self.wfile.write(json.dumps(res).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 8080), Handler)
    server.serve_forever()
APP

# 設定 systemd 服務
cat << 'SERVICE' > /etc/systemd/system/inspection.service
[Unit]
Description=Inspection Service
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/inspection/app.py
Restart=always

[Install]
WantedBy=multi-user.target
SERVICE

# 設定 Nginx 反向代理
cat << 'NGINX' > /etc/nginx/conf.d/inspection.conf
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGINX

# 啟動服務
systemctl daemon-reload
systemctl enable --now inspection
systemctl enable --now nginx
USERDATA

chmod +x "$OUTPUT_FILE"
echo "已成功生成 User Data 腳本於 $OUTPUT_FILE (Commit SHA: $COMMIT_SHA)"
