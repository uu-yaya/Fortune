# API 接口文档

## 1. 基础信息
- 默认地址：`http://127.0.0.1:8000`（容器/本地代理场景常用 `http://127.0.0.1:8001`）
- 编码：UTF-8
- 返回格式：JSON（页面路由返回 HTML）
- 鉴权：登录成功后通过 Cookie `jiyi_auth_token` 维持会话

## 2. FastAPI 自动文档
- Swagger UI：`GET /docs`
- OpenAPI JSON：`GET /openapi.json`

## 3. 页面路由

## 3.1 `GET /`
- 说明：主页入口
- 行为：未登录 302 到 `/login`；已登录渲染聊天页

## 3.2 `GET /index`
- 说明：聊天主页
- 行为：同 `/`

## 3.3 `GET /login`
- 说明：登录页
- 行为：已登录则 302 到 `/index`

## 3.4 `GET /register`
- 说明：注册页
- 行为：已登录则 302 到 `/index`

## 3.5 `GET /forgot-password`
- 说明：忘记密码页
- 行为：已登录则 302 到 `/index`；未登录渲染重置密码页面

## 3.6 `GET /reset-password`
- 说明：重置密码页别名
- 行为：与 `/forgot-password` 一致

## 4. 账户与鉴权接口

## 4.1 发送验证码 `POST /auth/send_code`
- 请求头：`Content-Type: application/json`
- 请求体：
```json
{
  "phone": "13800138000",
  "scene": "login"
}
```
- 参数说明：
  - `phone`：11 位中国大陆手机号
  - `scene`：验证码场景（`login`/`register`/`reset_password`/`default`）
- 成功响应：
```json
{
  "ok": true,
  "message": "验证码已发送",
  "debug_code": "123456",
  "ttl_seconds": 60
}
```
- 说明：`debug_code` 仅在 `SMS_DEBUG_CODE_ENABLED=true`（开发/演示）时返回；生产建议关闭。
- 失败响应：
  - `400`：手机号非法
  - `429`：发送过于频繁

## 4.2 验证码登录/注册 `POST /auth/verify`
- 请求头：`Content-Type: application/json`
- 登录请求体（`mode=login`）：
```json
{
  "phone": "13800138000",
  "code": "123456",
  "mode": "login"
}
```
- 注册请求体（`mode=register`）：
```json
{
  "phone": "13800138000",
  "code": "123456",
  "mode": "register",
  "password": "abc12345"
}
```
- 参数说明：
  - `mode`：`login` 或 `register`，其他值按 `login` 处理
  - `password`：仅注册必填，8-12 位字母或数字
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
  - `400`：验证码过期/错误
  - `400`：`mode=login` 但用户不存在
  - `400`：`mode=register` 但手机号已注册
  - `400`：注册密码不符合规则

## 4.3 账号密码登录 `POST /auth/login/password`
- 请求头：`Content-Type: application/json`
- 请求体：
```json
{
  "account": "JIYI-AB12CD34",
  "password": "abc12345"
}
```
- 成功响应（并设置 Cookie）：
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

## 4.4 忘记密码-校验验证码 `POST /auth/password/verify_code`
- 请求头：`Content-Type: application/json`
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

## 4.5 忘记密码-重置密码 `POST /auth/password/reset`
- 请求头：`Content-Type: application/json`
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
- 失败响应：
  - `400`：参数错误
  - `400`：未完成验证码校验

## 4.6 当前登录用户 `GET /auth/me`
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

## 4.7 退出登录 `POST /auth/logout`
- 鉴权：建议携带 Cookie
- 成功响应：
```json
{
  "ok": true,
  "message": "已退出登录"
}
```

## 5. 聊天接口

## 5.1 聊天 `POST /chat`
- 鉴权：需要 Cookie
- 请求体：
```json
{
  "query": "我想看下最近事业运",
  "session_id": "optional"
}
```
- 参数说明：
  - `query`：用户问题（必填）
  - `session_id`：兼容字段；后端不依赖该值进行会话绑定
- 成功响应：
```json
{
  "session_id": "后端会话ID",
  "output": "..."
}
```
- 失败响应：
  - `401`：未登录

## 6. 运维接口

## 6.1 质量指标 `GET /quality/metrics`
- Query 参数：
  - `days`：最近 N 天汇总，范围 `1..7`，默认 `1`
- 成功响应：
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
      "template_repeat_rate": 0.0
    },
    "series": []
  }
}
```

## 7. 知识入库接口

## 7.1 新增 URL 入库 `POST /add_urls`
- 参数位置：Query
- 参数：
  - `URL`：目标网页地址（必填）
  - `force_recreate`：是否重建集合（可选，默认 `false`）
- 示例：
  - `/add_urls?URL=https://example.com&force_recreate=false`
- 成功响应：
```json
{
  "ok": "添加成功！",
  "force_recreate": false
}
```

## 8. 状态码
- `200`：业务成功
- `302`：页面重定向
- `400`：请求参数错误
- `401`：未登录
- `429`：发送验证码过频

## 9. 调用示例

## 9.1 发送登录验证码
```bash
curl -c /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/auth/send_code" \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800138000","scene":"login"}'
```

## 9.2 验证码登录
```bash
curl -b /tmp/jiyi.cookie -c /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/auth/verify" \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800138000","code":"123456","mode":"login"}'
```

## 9.3 聊天
```bash
curl -b /tmp/jiyi.cookie -X POST "http://127.0.0.1:8001/chat" \
  -H "Content-Type: application/json" \
  -d '{"query":"你好"}'
```
