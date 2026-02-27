# API 接口文档

- 文档版本：v2.0
- 更新日期：2026-02-27
- 对齐代码：`server.py`

## 1. 基础信息

- 默认本地地址：`http://127.0.0.1:8000`
- Docker 默认地址：`http://127.0.0.1:8001`
- 编码：UTF-8
- 返回格式：JSON（页面路由返回 HTML）
- 鉴权方式：Cookie `jiyi_auth_token`

## 2. 自动文档

- Swagger：`GET /docs`
- OpenAPI：`GET /openapi.json`

## 3. 页面路由

### 3.1 `GET /`

- 说明：主页入口
- 行为：未登录 302 到 `/login`，已登录渲染 `index.html`

### 3.2 `GET /index`

- 说明：聊天主页
- 行为：同 `/`

### 3.3 `GET /login`

- 说明：登录页
- 行为：已登录 302 到 `/index`

### 3.4 `GET /register`

- 说明：注册页
- 行为：已登录 302 到 `/index`

### 3.5 `GET /forgot-password`

- 说明：忘记密码页

### 3.6 `GET /reset-password`

- 说明：忘记密码页别名

## 4. 鉴权与账户接口

### 4.1 `POST /auth/send_code`

- 说明：发送短信验证码
- 请求体：

```json
{
  "phone": "13800138000",
  "scene": "login"
}
```

- 参数：
  - `phone`：11位手机号
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

说明：`debug_code` 仅在 `SMS_DEBUG_CODE_ENABLED=true` 返回。

- 失败响应：
  - `400`：手机号不合法
  - `429`：发送过于频繁

### 4.2 `POST /auth/verify`

- 说明：验证码登录/注册
- 登录请求：

```json
{
  "phone": "13800138000",
  "code": "123456",
  "mode": "login"
}
```

- 注册请求：

```json
{
  "phone": "13800138000",
  "code": "123456",
  "mode": "register",
  "password": "abc12345"
}
```

- 成功响应（并设置 Cookie）：

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

- 常见失败：
  - `400`：验证码错误/过期
  - `400`：登录但手机号未注册
  - `400`：注册但手机号已存在
  - `400`：注册密码不符合 8-12 位字母数字规则

### 4.3 `POST /auth/login/password`

- 说明：账号密码登录
- 请求体：

```json
{
  "account": "JIYI-AB12CD34",
  "password": "abc12345"
}
```

- 成功响应：

```json
{
  "ok": true,
  "message": "登录成功",
  "phone": "13800138000",
  "user_id": "用户uuid",
  "short_account": "JIYI-AB12CD34"
}
```

- 失败响应：
  - `400`：账号或密码错误

### 4.4 `POST /auth/password/verify_code`

- 说明：忘记密码验证码校验
- 请求体：

```json
{
  "phone": "13800138000",
  "code": "123456"
}
```

- 成功响应：

```json
{
  "ok": true,
  "message": "验证码校验通过"
}
```

### 4.5 `POST /auth/password/reset`

- 说明：忘记密码重置
- 请求体：

```json
{
  "phone": "13800138000",
  "new_password": "abc12345",
  "confirm_password": "abc12345"
}
```

- 成功响应：

```json
{
  "ok": true,
  "message": "密码重置成功，请使用账号+密码登录"
}
```

### 4.6 `GET /auth/me`

- 说明：获取当前登录用户
- 鉴权：需要 Cookie

- 成功响应：

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

- 未登录响应：

```json
{
  "ok": false,
  "message": "未登录"
}
```

### 4.7 `POST /auth/logout`

- 说明：退出登录
- 响应：

```json
{
  "ok": true,
  "message": "已退出登录"
}
```

## 5. 聊天接口

### 5.1 `POST /chat`

- 说明：统一聊天问答入口
- 鉴权：需要 Cookie
- 请求体：

```json
{
  "query": "我想看下最近事业运",
  "session_id": "optional-client-id"
}
```

说明：`session_id` 为兼容字段，后端实际以用户 UUID 维护会话。

- 成功响应：

```json
{
  "session_id": "服务端会话ID",
  "output": "..."
}
```

- 未登录响应（401）：

```json
{
  "output": "请先登录后再继续聊天。"
}
```

## 6. 质量与运维接口

### 6.1 `GET /quality/metrics`

- 说明：质量指标看板
- Query：`days=1..7`，默认 `1`

- 成功响应示例：

```json
{
  "ok": true,
  "data": {
    "days": 1,
    "totals": {},
    "rates": {
      "fortune_route_hit_rate": 0.0,
      "fortune_tool_success_rate": 0.0,
      "fortune_field_completeness_rate": 0.0,
      "profile_echo_violation_rate": 0.0,
      "template_repeat_rate": 0.0,
      "unique_output_rate": 0.0,
      "observability_coverage": 0.0
    },
    "series": []
  }
}
```

## 7. 知识入库接口

### 7.1 `POST /add_urls`

- 说明：抓取网页并写入向量库
- 参数位置：Query
- 参数：
  - `URL`：必填，目标网页地址
  - `force_recreate`：可选，默认 `false`

示例：

```text
/add_urls?URL=https://example.com&force_recreate=false
```

- 成功响应：

```json
{
  "ok": "添加成功！",
  "force_recreate": false
}
```

## 8. 状态码约定

- `200`：成功
- `302`：页面重定向
- `400`：请求参数错误
- `401`：未登录
- `429`：验证码发送过频

## 9. 调用示例

### 9.1 发送验证码

```bash
curl -c /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/auth/send_code" \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800138000","scene":"login"}'
```

### 9.2 验证码登录

```bash
curl -b /tmp/jiyi.cookie -c /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/auth/verify" \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800138000","code":"123456","mode":"login"}'
```

### 9.3 聊天

```bash
curl -b /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/chat" \
  -H "Content-Type: application/json" \
  -d '{"query":"你好"}'
```
