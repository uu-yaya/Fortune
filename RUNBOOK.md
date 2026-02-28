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