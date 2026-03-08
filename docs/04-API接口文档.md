# API 接口文档

- 文档版本：v3.0
- 更新日期：2026-03-08
- 对齐代码：`server.py`

## 1. 基础信息

- 本地默认地址：`http://127.0.0.1:8000`
- Docker Compose 默认地址：`http://127.0.0.1:8001`
- 编码：UTF-8
- API 返回：JSON
- 页面路由返回：HTML
- 鉴权方式：Cookie `jiyi_auth_token`

## 2. 页面路由

### `GET /`

- 说明：主页入口
- 行为：未登录 `302 -> /login`，已登录渲染 `index.html`

### `GET /index`

- 说明：聊天主页
- 行为：同 `/`

### `GET /login`

- 说明：登录页
- 行为：已登录 `302 -> /index`

### `GET /register`

- 说明：注册页
- 行为：已登录 `302 -> /index`

### `GET /forgot-password`

- 说明：忘记密码页

### `GET /reset-password`

- 说明：重置密码页别名

## 3. 鉴权与账户接口

### `POST /auth/send_code`

- 说明：发送短信验证码
- 请求体：

```json
{
  "phone": "13800138000",
  "scene": "login"
}
```

- 参数：
  - `phone`：11 位手机号
  - `scene`：`login` / `register` / `reset_password` / `default`

- 成功响应：

```json
{
  "ok": true,
  "message": "验证码已发送",
  "ttl_seconds": 60,
  "debug_code": "123456"
}
```

说明：`debug_code` 只在 `SMS_DEBUG_CODE_ENABLED=true` 时返回。

### `POST /auth/verify`

- 说明：验证码登录/注册
- 请求体：

```json
{
  "phone": "13800138000",
  "code": "123456",
  "mode": "login"
}
```

- 注册时可增加：

```json
{
  "password": "abc12345"
}
```

- 成功响应：

```json
{
  "ok": true,
  "message": "登录成功",
  "phone": "13800138000",
  "mode": "login",
  "user_id": "用户uuid",
  "short_account": "JIYI-AB12CD34"
}
```

### `POST /auth/login/password`

- 说明：账号密码登录
- 请求体：

```json
{
  "account": "JIYI-AB12CD34",
  "password": "abc12345"
}
```

### `POST /auth/password/verify_code`

- 说明：忘记密码前的验证码校验

### `POST /auth/password/reset`

- 说明：忘记密码重置

### `GET /auth/me`

- 说明：获取当前登录用户与已记住的基础资料
- 鉴权：需要 Cookie
- 成功响应示例：

```json
{
  "ok": true,
  "user": {
    "phone": "13800138000",
    "user_id": "用户uuid",
    "short_account": "JIYI-AB12CD34"
  },
  "profile": {
    "name": "张三",
    "preferred_name": "阿星",
    "birthdate": "2001-08-15"
  }
}
```

### `POST /auth/logout`

- 说明：退出登录

## 4. 聊天接口

### `POST /chat`

- 说明：统一聊天问答入口
- 鉴权：需要 Cookie
- 请求体：

```json
{
  "query": "分析一下我今年的运势",
  "session_id": "optional-client-id"
}
```

说明：`session_id` 是兼容字段，后端实际使用当前登录用户 UUID 维护会话。

- 成功响应：

```json
{
  "session_id": "后端会话ID",
  "output": "返回给用户的文本"
}
```

- 关键行为：
  1. 未登录返回 `401`
  2. 快捷问答优先于 Agent fallback
  3. 命理场景会自动补资料、做时间窗口控制、做输出脱敏
  4. 身份事实问答（姓名/生日/时辰）优先走事实直答

- 常见问法类型：
  - 普通聊天：`你好`、`你在吗`
  - 时间问答：`今天几号`、`我这两天该做什么`
  - 命理问答：`我的正缘`、`我接下来一个月的主题是什么`
  - 身份事实：`我叫什么`、`我的生日是？`、`我的出生时间是？`
  - 解梦 / 星座 / 占卜

## 5. 质量与知识接口

### `GET /quality/metrics`

- 说明：查看最近 `N` 天的质量指标聚合
- 参数：
  - `days`：`1-7`，默认 `1`
- 成功响应：

```json
{
  "ok": true,
  "data": {
    "days": 1
  }
}
```

### `POST /add_urls`

- 说明：抓取网页并写入本地 Qdrant 向量库
- Query 参数：
  - `URL`：待抓取网页地址
  - `force_recreate`：是否重建集合，默认 `false`

- 成功响应：

```json
{
  "ok": "添加成功！",
  "force_recreate": false
}
```

## 6. 错误与兼容说明

1. 用户态回复默认不应暴露：
   - 内部工具名
   - 函数名
   - 完整生日 + 出生时段原文
2. 文档中的默认健康检查建议使用：
   - `GET /index`
   - 不建议把 `GET /docs` 作为唯一健康检查信号
3. 如果部署时启用了 `password-only` 网关：
   - `/auth/send_code`、`/auth/verify`、`/auth/password/*` 可能被网关拦截
   - 这属于专项部署策略，不是应用主线默认行为
