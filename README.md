# 吉伊大师命理咨询系统

基于 FastAPI + LangChain + MySQL + Redis + Qdrant 的命理问答系统，支持验证码注册登录、账号密码登录、资料抽取、命理问答、质量指标统计与回归门禁。

## 1. 功能概览

- 鉴权与账户
  - 手机号验证码登录/注册
  - 账号密码登录
  - 忘记密码（验证码校验 + 密码重置）
- 智能问答
  - 普通聊天、时间快捷回复
  - 命理问答（资料补齐、命理工具链路）
  - 星座问答、解梦、占卜
- 数据与知识
  - MySQL 持久化用户、资料、会话审计、密码重置日志
  - Redis 存储会话、验证码、冷却计时、短期聊天历史、质量指标
  - 本地 Qdrant 向量库支持网页知识入库
- 质量治理
  - `/quality/metrics` 质量指标看板
  - `scripts/fortune_regression.py` 回归脚本
  - `scripts/quality_gate.py` 门禁脚本

## 2. 技术栈

- 后端：Python 3.11、FastAPI、Uvicorn、LangChain
- 数据：MySQL 8、Redis、Qdrant（本地持久化）
- 前端：Jinja2 模板 + 原生 JavaScript
- 日志：loguru

## 3. 项目结构

```text
fortune-telling/
├── server.py                  # 核心后端：路由 + 业务编排 + AI链路
├── config.py                  # 环境变量配置
├── models.py                  # LLM / Embedding 客户端工厂
├── mytools.py                 # Agent 工具函数
├── sql/001_init_auth_schema.sql
├── scripts/
│   ├── fortune_regression.py  # 命理回归脚本
│   └── quality_gate.py        # 质量门禁脚本
├── templates/                 # 页面模板
├── static/                    # 静态资源（JS/CSS）
├── docs/                      # 设计/接口/测试与历史报告
├── docker-compose.yml
└── README.md
```

## 4. 快速开始

### 4.1 本地运行

1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 配置环境变量

```bash
cp .env.example .env
```

关键变量（详见 `.env.example`）：

- `REDIS_URL`
- `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_DB` / `MYSQL_USER` / `MYSQL_PASSWORD`
- `DASHSCOPE_API_KEY`（模型）
- `SERPAPI_API_KEY`（可选）
- `YUANFENJU_API_KEY`（命理工具）
- `SMS_DEBUG_CODE_ENABLED`（开发环境可为 `true`）
- `SMS_PROVIDER`（`mock` 或 `aliyun`）
- `SMS_ALIYUN_ACCESS_KEY_ID` / `SMS_ALIYUN_ACCESS_KEY_SECRET`
- `SMS_ALIYUN_SIGN_NAME` / `SMS_ALIYUN_TEMPLATE_CODE`

3. 启动服务

```bash
python server.py
```

默认监听：`http://127.0.0.1:8000`

4. 访问入口

- 前端：`http://127.0.0.1:8000/index`
- API文档：`http://127.0.0.1:8000/docs`

### 4.1.1 本机终端提问流程（登录/注册 → 发送聊天）
```bash
# 运行server
REDIS_URL=redis://127.0.0.1:6380/ \
MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_DB=fortune_telling \
MYSQL_USER=fortune_app MYSQL_PASSWORD=fortune_app_dev \
SMS_DEBUG_CODE_ENABLED=true \
./.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000
```
```bash
# 1) 发送验证码（开发环境会返回 debug_code）
curl -sS -X POST http://127.0.0.1:8000/auth/send_code \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13800138000","scene":"register"}'

# 2) 验证/注册（用上一步的 debug_code）
curl -sS -X POST http://127.0.0.1:8000/auth/verify \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13800138000","code":"123456","mode":"register","password":"abc12345"}' \
  -c /tmp/jiyi.cookie

# 3) 发送聊天
curl -sS -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"分析一下我今年的运势"}' \
  -b /tmp/jiyi.cookie
```

已注册用户可将第 2 步的 `mode` 改为 `login`。

### 4.2 Docker Compose 运行（推荐）

```bash
docker compose -p zhipo-numerology up -d --build
```

默认端口映射：

- 应用：`127.0.0.1:8001 -> 8000`
- Redis：`127.0.0.1:6380 -> 6379`
- MySQL：`127.0.0.1:3307 -> 3306`

停止服务：

```bash
docker compose -p zhipo-numerology down
```

### 4.3 Password-Only 首发（无短信）

当短信通道未开通时，可先上线“账号密码登录 + 聊天”模式：

1. 使用环境模板：

```bash
cp deploy/env/.env.password-only.example .env
```

2. 保持以下配置：

- `SMS_PROVIDER=mock`
- `SMS_DEBUG_CODE_ENABLED=false`

3. 配置 Nginx password-only 网关模板：

- `deploy/nginx/password-only.conf.example`

4. 批量预置账号（容器内执行）：

```bash
docker compose exec -T numerology python scripts/bootstrap_password_only_accounts.py \
  --entry 13800138000:TempA123 \
  --entry 13900139000:TempB123
```

5. 验收脚本：

```bash
BASE_URL=https://你的域名 ACCOUNT=你的账号 PASSWORD=你的密码 \
bash scripts/password_only_acceptance.sh
```

完整上线流程见：

- `docs/31-password-only-ecs-runbook-2026-02-28.md`

## 5. 测试与质量门禁

1. 命理回归

```bash
python3 scripts/fortune_regression.py --base-url http://127.0.0.1:8001 --max-cases 24
```

2. 质量门禁

```bash
python3 scripts/quality_gate.py --base-url http://127.0.0.1:8001 --days 1
```

## 6. 文档导航

核心规范文档：

- `docs/01-概要设计说明书.md`
- `docs/02-详细设计说明书.md`
- `docs/03-数据库设计说明书.md`
- `docs/04-API接口文档.md`
- `docs/05-测试用例.md`
- `docs/README.md`（全部文档导航）

## 7. 安全与上线注意事项

- 生产环境必须关闭 `SMS_DEBUG_CODE_ENABLED`，避免回传验证码。
- 生产环境请设置 `SMS_PROVIDER=aliyun`，并配置阿里云短信签名、模板与密钥。
- 若采用 password-only 首发，需在网关层禁用短信/注册/找回密码入口。
- 建议在 HTTPS 场景下启用更严格 Cookie 策略（如 `Secure`）。
- 对外部模型/工具依赖做好降级与熔断策略。
- `.env` 中包含敏感信息，不应提交到仓库。
