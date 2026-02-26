# MySQL + Redis 数据存储设计方案

## 1. 目标与原则
- 支持两种登录方式：`手机号+验证码`、`账号+密码`
- 支持忘记密码：`手机号+验证码`重置
- 一个手机号唯一对应一个用户
- 注册自动生成：`uuid`（系统唯一标识）、`account`（短账号）
- 用户资料与历史可跨会话恢复，不依赖浏览器本地存储
- MySQL 作为真数据源（SoT），Redis 作为高性能临时态与缓存

---

## 2. 存储分工

### 2.1 MySQL（持久层）
- 用户主数据（账号、手机号、密码哈希）
- 用户画像资料（姓名、出生日期、出生时刻等）
- 会话审计记录
- 聊天消息持久化（可选异步落库）
- 短信发送/校验审计日志
- 密码重置审计日志

### 2.2 Redis（高速层）
- 登录态 Session（token 映射用户）
- 验证码与冷却计时
- 短期聊天上下文（最近 N 轮）
- 限流计数器
- 热点资料缓存

---

## 3. MySQL 逻辑模型

## 3.1 `users` 用户主表
- `id` bigint PK auto_increment
- `uuid` char(32) not null unique
- `account` varchar(24) not null unique
- `phone` varchar(20) not null unique
- `password_hash` varchar(255) not null
- `avatar_url` varchar(255) null
- `status` tinyint not null default 1
- `created_at` datetime not null
- `updated_at` datetime not null

说明：
- `account` 自动生成，建议格式：`JIYI-` + 6~8位大写字母数字。
- `password_hash` 必须为哈希值（bcrypt/argon2），不落明文密码。

## 3.2 `user_profile` 用户资料表
- `user_id` bigint PK（FK -> users.id）
- `name` varchar(64) null
- `birth_date` date null
- `birth_time` time null
- `gender` tinyint null
- `timezone` varchar(64) null default 'Asia/Shanghai'
- `profile_json` json null（扩展资料）
- `updated_at` datetime not null

## 3.3 `auth_sessions` 登录会话表（审计）
- `id` bigint PK auto_increment
- `user_id` bigint not null（FK -> users.id）
- `token_hash` char(64) not null unique
- `login_type` enum('sms','password') not null
- `device_info` varchar(255) null
- `ip` varchar(64) null
- `expires_at` datetime not null
- `created_at` datetime not null
- `revoked_at` datetime null

## 3.4 `chat_messages` 聊天消息表（建议异步写入）
- `id` bigint PK auto_increment
- `user_id` bigint not null
- `session_id` varchar(64) not null
- `role` enum('user','assistant','system') not null
- `content` text not null
- `meta_json` json null
- `created_at` datetime not null

索引：
- `idx_chat_user_time (user_id, created_at)`
- `idx_chat_session_time (session_id, created_at)`

## 3.5 `sms_code_logs` 验证码日志表
- `id` bigint PK auto_increment
- `phone` varchar(20) not null
- `scene` enum('login','register','reset_password') not null
- `code_hash` char(64) not null
- `status` enum('sent','verified','expired','failed') not null
- `created_at` datetime not null
- `verified_at` datetime null

## 3.6 `password_reset_logs` 密码重置日志表
- `id` bigint PK auto_increment
- `user_id` bigint not null
- `phone` varchar(20) not null
- `reset_at` datetime not null
- `ip` varchar(64) null
- `user_agent` varchar(255) null

---

## 4. MySQL 建表 SQL（参考）

```sql
CREATE TABLE IF NOT EXISTS users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  uuid CHAR(32) NOT NULL UNIQUE,
  account VARCHAR(24) NOT NULL UNIQUE,
  phone VARCHAR(20) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  avatar_url VARCHAR(255) NULL,
  status TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_profile (
  user_id BIGINT PRIMARY KEY,
  name VARCHAR(64) NULL,
  birth_date DATE NULL,
  birth_time TIME NULL,
  gender TINYINT NULL,
  timezone VARCHAR(64) NULL DEFAULT 'Asia/Shanghai',
  profile_json JSON NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_profile_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS auth_sessions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  token_hash CHAR(64) NOT NULL UNIQUE,
  login_type ENUM('sms','password') NOT NULL,
  device_info VARCHAR(255) NULL,
  ip VARCHAR(64) NULL,
  expires_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at DATETIME NULL,
  INDEX idx_auth_user_created (user_id, created_at),
  INDEX idx_auth_expires (expires_at),
  CONSTRAINT fk_auth_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_messages (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  role ENUM('user','assistant','system') NOT NULL,
  content TEXT NOT NULL,
  meta_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_chat_user_time (user_id, created_at),
  INDEX idx_chat_session_time (session_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sms_code_logs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  phone VARCHAR(20) NOT NULL,
  scene ENUM('login','register','reset_password') NOT NULL,
  code_hash CHAR(64) NOT NULL,
  status ENUM('sent','verified','expired','failed') NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  verified_at DATETIME NULL,
  INDEX idx_sms_phone_scene_time (phone, scene, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS password_reset_logs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  phone VARCHAR(20) NOT NULL,
  reset_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ip VARCHAR(64) NULL,
  user_agent VARCHAR(255) NULL,
  INDEX idx_reset_user_time (user_id, reset_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 5. Redis Key 设计

## 5.1 登录会话
- `auth:session:{token}` -> JSON `{user_id, uuid, account, phone}`，TTL=`30d`

## 5.2 验证码与冷却
- `auth:sms:{scene}:{phone}` -> `code_hash`，TTL=`300s`
- `auth:sms:cooldown:{scene}:{phone}` -> `1`，TTL=`60s`

## 5.3 忘记密码校验通过态
- `auth:pwdreset:verified:{phone}` -> `1`，TTL=`600s`

## 5.4 聊天上下文
- `chat:ctx:{uuid}` -> 最近 N 轮对话（List/JSON），TTL=`7d~30d`

## 5.5 热缓存
- `user:profile:{user_id}` -> profile json，TTL=`10m~60m`

## 5.6 限流
- `ratelimit:sms:{phone}:{yyyyMMddHHmm}` -> 计数，TTL=`120s~300s`
- `ratelimit:chat:{user_id}:{yyyyMMddHHmm}` -> 计数，TTL=`120s`

---

## 6. 密码规则与安全要求

## 6.1 密码规则（注册/重置）
- 正则：`^[A-Za-z0-9]{8,12}$`
- 仅字母数字
- 最少8位，最多12位

## 6.2 安全
- 密码只存哈希（建议 Argon2id，其次 bcrypt）
- Token 仅存哈希到 MySQL（可审计），Redis 存在线态
- Cookie：`HttpOnly + Secure + SameSite=Lax`
- 验证码错误次数限制（例如 5 次锁定 10 分钟）
- 关键接口限流（按手机号、IP 双维度）

---

## 7. 一致性与容灾
- MySQL 为最终一致基准；Redis 丢失可由 MySQL 回填
- 写资料采用：先写 MySQL，再删/刷新 Redis 缓存
- 聊天消息可先写 Redis，异步批量落 MySQL
- 定时任务：
  - 清理过期会话审计
  - 清理历史验证码日志
  - 归档长期聊天记录

---

## 8. 与当前项目的迁移步骤
1. 新增 MySQL 连接与 DAO 层（用户、资料、认证、消息）
2. 保留现有 API 路径，替换内存字典实现
3. 把 `output/auth_state.json` 迁移到 `users/user_profile`
4. 把验证码与登录态切换为 Redis Key
5. `chat` 统一按 `uuid` 拉资料与历史，前端不再承担会话持久化职责
6. 上线前做压测与风控阈值调优（验证码、登录、聊天）

---

## 9. 验收标准
- 清空浏览器本地存储后，重新登录仍可恢复账号与历史资料
- 同一手机号不可重复注册
- 账号+密码登录与手机号+验证码登录均可用
- 忘记密码流程可闭环，且旧密码失效
- 聊天可读取历史上下文与用户资料，跨 session 连续
