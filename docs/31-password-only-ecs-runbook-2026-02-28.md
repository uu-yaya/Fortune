# 31-Password-Only 专项上线手册（ECS + Compose）

- 更新日期：2026-03-08
- 适用范围：仅在“短信能力未开通，需要临时切成账号密码首发模式”时使用
- 注意：这不是当前项目默认部署流程；默认主线仍是完整鉴权能力

## 1. 何时使用

仅在以下条件同时成立时使用本手册：

1. 短信通道尚未开通或暂时不可用
2. 需要先上线可登录、可聊天的最小版本
3. 接受通过网关禁用注册/短信/找回密码入口

如果不是上述场景，请不要按本文档部署，改看常规 ECS / Compose 部署流程。

## 2. 部署策略

核心思路：

- 应用代码尽量不改
- 通过 `.env` 与网关规则切换到 password-only
- 账号由管理员预置

## 3. 关键配置

必须确认：

- `SMS_PROVIDER=mock`
- `SMS_DEBUG_CODE_ENABLED=false`
- `MYSQL_ROOT_PASSWORD` 已设强密码
- 已配置应用运行所需模型/数据库/缓存环境变量

## 4. 核心步骤

1. 准备 ECS / Docker / Compose / Nginx
2. 拉取项目到 `/opt/fortune-telling`
3. 复制并修改 `deploy/env/.env.password-only.example`
4. `docker compose up -d --build`
5. 应用 password-only Nginx 配置
6. 预置首批账号
7. 执行专项验收脚本 `scripts/password_only_acceptance.sh`

## 5. 专项脚本

- `scripts/bootstrap_password_only_accounts.py`
- `scripts/reset_user_password.py`
- `scripts/password_only_acceptance.sh`

## 6. 回滚

1. 去掉短信/注册/找回密码的网关拦截
2. 恢复常规入口
3. 重新加载 Nginx
4. 按需切回常规 `.env` 配置

## 7. 备注

- 如果短信已经可用，请不要继续维护 password-only 专项网关为默认方案。
- 如果要长期保留此模式，建议后续把相关部署材料移到 `deploy/` 或独立运维文档目录，而不是继续扩展在通用产品文档里。
