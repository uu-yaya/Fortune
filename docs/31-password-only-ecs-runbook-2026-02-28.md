# 31-Password-Only 首发上线手册（ECS + Compose）-2026-02-28

## 1. 目标

在未接短信通道前，系统以 `账号密码登录 + 聊天` 对外提供服务，并通过 Nginx 禁用短信、注册和找回密码入口。

## 2. 适用范围

- 部署形态：阿里云 ECS（Ubuntu）+ Docker Compose
- 当前策略：不改应用代码，仅通过 `.env` 与 Nginx 配置实现 password-only

## 3. 上线前准备

### 3.1 云侧

1. 新建 ECS（建议 `2C4G`，系统盘 `>=60GB`）。
2. 绑定公网 IP。
3. 安全组只放行 `22/80/443`，禁止暴露 `3306/6379`。
4. 域名 `A` 记录解析到 ECS 公网 IP。

### 3.2 服务器初始化

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

## 4. 应用部署

```bash
sudo mkdir -p /opt/fortune-telling
sudo chown -R "$USER":"$USER" /opt/fortune-telling
cd /opt/fortune-telling
git clone <your_repo_url> .
```

### 4.1 生产环境变量

```bash
cp deploy/env/.env.password-only.example .env
```

必须修改：

- `DASHSCOPE_API_KEY`
- `MYSQL_ROOT_PASSWORD`
- 可选：`YUANFENJU_API_KEY`、`SERPAPI_API_KEY`

必须保持：

- `SMS_PROVIDER=mock`
- `SMS_DEBUG_CODE_ENABLED=false`

### 4.2 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 numerology
```

## 5. Nginx Password-Only 网关

1. 复制模板并替换域名、证书路径：

```bash
sudo cp deploy/nginx/password-only.conf.example /etc/nginx/conf.d/fortune-telling.conf
sudo vim /etc/nginx/conf.d/fortune-telling.conf
```

2. 语法检查并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

> 配置模板路径：`deploy/nginx/password-only.conf.example`

## 6. HTTPS

### 6.1 Let’s Encrypt（示例）

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d <your_domain>
```

### 6.2 强制 HTTPS

模板已内置 `80 -> 443` 跳转，证书部署后再次执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 7. 无短信账号管理

### 7.1 批量预置账号

```bash
docker compose exec -T numerology python scripts/bootstrap_password_only_accounts.py \
  --entry 13800138000:TempA123 \
  --entry 13900139000:TempB123
```

也可从文件导入：

```bash
cat > /tmp/password_only_seed.txt <<'EOF'
13800138000,TempA123
13900139000,TempB123
EOF

docker compose exec -T numerology python scripts/bootstrap_password_only_accounts.py \
  --seed-file /tmp/password_only_seed.txt
```

### 7.2 密码重置（工单人工流程）

```bash
docker compose exec -T numerology python scripts/reset_user_password.py \
  --account JIYI-XXXXXXXX \
  --new-password NewPass88
```

或按手机号：

```bash
docker compose exec -T numerology python scripts/reset_user_password.py \
  --phone 13800138000 \
  --new-password NewPass88
```

## 8. 验收

### 8.1 一键验收脚本

```bash
BASE_URL=https://<your_domain> \
ACCOUNT=JIYI-XXXXXXXX \
PASSWORD=TempA123 \
bash scripts/password_only_acceptance.sh
```

### 8.2 人工补充验收点

1. `GET /login` 返回 200。
2. `POST /auth/login/password` 返回 200，且下发 `jiyi_auth_token`。
3. 带 Cookie 调 `POST /chat` 返回 200 且包含 `output`。
4. 短信接口返回 410。
5. `/register`、`/forgot-password`、`/reset-password` 跳转到 `/login`。
6. 未登录访问 `/index` 跳转 `/login`。

## 9. 运维与备份（最低配）

1. 每日检查容器状态：
   - `docker compose ps`
   - `docker compose logs --tail=200 numerology`
2. 数据库备份（建议每日）：
   - `mysqldump` 备份后上传 OSS。
3. 告警建议：
   - ECS CPU/内存/磁盘
   - Nginx 5xx
   - 容器重启次数

## 10. 回滚

1. 去掉 Nginx 对 `/auth/send_code`、`/auth/verify`、`/auth/password/*` 的 410 拦截。
2. 去掉 `/register|/forgot-password|/reset-password` 到 `/login` 的 302 规则。
3. 按需恢复 `.env` 中短信配置（未来开通短信时切 `SMS_PROVIDER=aliyun`）。
4. 重载 Nginx，重启容器：

```bash
sudo nginx -t
sudo systemctl reload nginx
docker compose up -d
```
