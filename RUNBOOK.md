# RUNBOOK（简易版）

## 0. 进入项目

```bash
cd /Users/yayauu/PycharmProjects/fortune-telling
```

## 1. Docker 自检

```bash
docker info
```

如果失败（daemon 未启动）：

```bash
sudo /Applications/Docker.app/Contents/MacOS/install config --user "$(id -un)"
sudo /Applications/Docker.app/Contents/MacOS/install vmnetd
sudo /Applications/Docker.app/Contents/MacOS/install socket-symlink-on-startup
pkill -f Docker || true
open -a Docker
sleep 25
docker info
```

## 2. 启动服务（compose）

```bash
docker compose -p zhipo-numerology up -d --build
```

## 3. 查看状态与日志

```bash
docker compose -p zhipo-numerology ps
docker compose -p zhipo-numerology logs -f numerology
```

## 4. 访问地址

- 前端: http://127.0.0.1:8001/index  
- 文档: http://127.0.0.1:8001/docs
- 公网（ngrok，记录于 2026-02-26）: https://steamiest-worried-toby.ngrok-free.dev

## 5. 快速接口验证

```bash
curl -sS -X POST http://127.0.0.1:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"你好","session_id":"smoke001"}'
```

## 6. 停止服务

```bash
docker compose -p zhipo-numerology down
```

## 7. 清理（可选）

仅在确认不需要 Redis 数据时执行：

```bash
docker compose -p zhipo-numerology down -v
```

## 8. Cloudflare 固定域名（命名隧道）

1. 在 Cloudflare Zero Trust 创建 `Named Tunnel`（Cloudflared）。
2. 为该 Tunnel 添加 `Public Hostname`（你的域名），并把后端指向：
   - URL: `http://host.docker.internal:8001`
3. 将 Tunnel Token 写入 `.env`：

```bash
CLOUDFLARED_TUNNEL_TOKEN=你的token
```

4. 启动命名隧道容器：

```bash
docker compose -f docker-compose.tunnel.named.yml --env-file .env up -d
```

5. 查看运行状态与日志：

```bash
docker compose -f docker-compose.tunnel.named.yml ps
docker compose -f docker-compose.tunnel.named.yml logs -f cloudflared
```

6. 停止隧道：

```bash
docker compose -f docker-compose.tunnel.named.yml down
```

## 9. Password-Only（无短信首发）

1. 使用环境模板并确认核心开关：

```bash
cp deploy/env/.env.password-only.example .env
```

必须保持：

- `SMS_PROVIDER=mock`
- `SMS_DEBUG_CODE_ENABLED=false`

2. 启动服务：

```bash
docker compose -p zhipo-numerology up -d --build
```

3. 部署 Nginx password-only 模板：

- 配置文件：`deploy/nginx/password-only.conf.example`

4. 预置账号（容器内）：

```bash
docker compose exec -T numerology python scripts/bootstrap_password_only_accounts.py \
  --entry 13800138000:TempA123
```

5. 一键验收：

```bash
BASE_URL=https://你的域名 ACCOUNT=你的账号 PASSWORD=你的密码 \
bash scripts/password_only_acceptance.sh
```

完整手册见：`docs/31-password-only-ecs-runbook-2026-02-28.md`

## 一键巡检
```bash
chmod +x /opt/fortune-telling/scripts/daily_check.sh
/opt/fortune-telling/scripts/daily_check.sh
```

## ECS更新上线
```bash
cd /opt/fortune-telling && \
git pull --ff-only origin main && \
docker compose up -d --build && \
docker compose ps && \
curl -sS -o /dev/null -w "login=%{http_code}\n" http://127.0.0.1:8001/login && \
curl -sS -o /dev/null -w "docs=%{http_code}\n" http://127.0.0.1:8001/docs && \
docker compose logs --tail=80 numerology
```

## 实时监控
```bash
cd /opt/fortune-telling && \
cat >/opt/fortune-telling/scripts/tail_chat_by_uuid.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
USER_UUID="${1:-}"
if [[ -z "$USER_UUID" ]]; then
  echo "Usage: $0 <user_uuid>"
  exit 2
fi

cd /opt/fortune-telling
docker compose exec -T numerology env USER_UUID="$USER_UUID" python -u - <<'PY'
import os, json, time, hashlib, redis

u = os.environ["USER_UUID"]
r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/"), decode_responses=True)
key = f"message_store:{u}"

def decode_msg(raw: str):
    try:
        d = json.loads(raw)
        role = d.get("type", "raw")
        content = ((d.get("data") or {}).get("content") or "")
        return role, content
    except Exception:
        return "raw", raw

rows = r.lrange(key, 0, -1)  # new -> old
print(f"key={key} len={len(rows)}", flush=True)
for i, raw in enumerate(reversed(rows)):  # old -> new
    role, content = decode_msg(raw)
    print(f"[{i}] {role}: {content}", flush=True)

seen = {hashlib.md5(x.encode('utf-8')).hexdigest() for x in rows}
print("tailing... Ctrl+C stop", flush=True)

while True:
    latest = r.lrange(key, 0, 30)  # new -> old
    fresh = [x for x in latest if hashlib.md5(x.encode('utf-8')).hexdigest() not in seen]
    if fresh:
        for raw in reversed(fresh):  # old -> new
            role, content = decode_msg(raw)
            print(f"{role}: {content}", flush=True)
            seen.add(hashlib.md5(raw.encode('utf-8')).hexdigest())
    time.sleep(1)
PY
EOF
```
```bash
chmod +x /opt/fortune-telling/scripts/tail_chat_by_uuid.sh && \
```
```bash
/opt/fortune-telling/scripts/tail_chat_by_uuid.sh 9bed834ed4f8482bb406ba772a02eec1
```

## 生成新用户
```bash
cd /opt/fortune-telling
docker compose exec -T numerology python - <<'PY'
from server import _get_user_by_phone, _create_user_by_phone

seed = [
    ("13800138101", "TempA101"),
    ("13800138102", "TempA102"),
    ("13800138103", "TempA103"),
    ("13800138104", "TempA104"),
    ("13800138105", "TempA105"),
    ("13800138106", "TempA106"),
    ("13800138107", "TempA107"),
    ("13800138108", "TempA108"),
    ("13800138109", "TempA109"),
    ("13800138110", "TempA110"),
]

print("phone,account,init_password")
for phone, pwd in seed:
    u = _get_user_by_phone(phone)
    if not u:
        _create_user_by_phone(phone, pwd)
        u = _get_user_by_phone(phone)
    print(f"{phone},{u['account']},{pwd}")
PY
```

## 监控所有用户
```bash 
/opt/fortune-telling/scripts/tail_chat_multi.sh --all
```