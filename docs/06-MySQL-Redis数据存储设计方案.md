# MySQL + Redis 数据存储设计方案

- 更新日期：2026-03-08
- 目标：说明当前主线实现下，MySQL 与 Redis 的分工、键设计和运维要求。

## 1. 目标与原则

- 支持两种登录方式：`手机号+验证码`、`账号+密码`
- 支持忘记密码：`手机号+验证码`重置
- 一个手机号唯一对应一个用户
- 注册自动生成：`uuid`、`account`
- 用户资料与历史可跨会话恢复，不依赖浏览器本地存储
- MySQL 作为真数据源，Redis 作为高性能临时态与缓存
- 命理主链路当前把 `gender` 视为关键资料字段

## 2. 存储分工

### 2.1 MySQL（持久层）

- 用户主数据（账号、手机号、密码哈希）
- 用户画像资料（姓名、出生日期、出生时刻）
- `profile_json` 扩展资料（称呼偏好、性别等）
- 会话审计记录
- 聊天消息持久化（预留）
- 短信发送/校验审计日志
- 密码重置审计日志

### 2.2 Redis（高速层）

- 登录态 Session（token 映射用户）
- 验证码与冷却计时
- 短期聊天上下文（最近 N 轮）
- provider flag
- merchant probe 缓存
- 质量指标聚合

## 3. MySQL 逻辑模型

### 3.1 `users` 用户主表

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

- `account` 自动生成，格式为 `JIYI-XXXXXXXX`
- `password_hash` 存哈希，不落明文密码

### 3.2 `user_profile` 用户资料表

- `user_id` bigint PK（FK -> users.id）
- `name` varchar(64) null
- `birth_date` date null
- `birth_time` time null
- `gender` tinyint null
- `timezone` varchar(64) null default 'Asia/Shanghai'
- `profile_json` json null
- `updated_at` datetime not null

说明：

- 代码当前主读 `name / birth_date / birth_time + profile_json`
- `profile_json` 至少承载：
  - `preferred_name`
  - `preferred_name_confidence`
  - `name_confidence`
  - `gender`

### 3.3 `auth_sessions` 登录会话表（审计）

- `id` bigint PK auto_increment
- `user_id` bigint not null（FK -> users.id）
- `token_hash` char(64) not null unique
- `login_type` enum('sms','password') not null
- `device_info` varchar(255) null
- `ip` varchar(64) null
- `expires_at` datetime not null
- `created_at` datetime not null
- `revoked_at` datetime null

### 3.4 `chat_messages` 聊天消息表（预留）

- `id` bigint PK auto_increment
- `user_id` bigint not null
- `session_id` varchar(64) not null
- `role` enum('user','assistant','system') not null
- `content` text not null
- `meta_json` json null
- `created_at` datetime not null

### 3.5 `sms_code_logs` 验证码日志表

- `id` bigint PK auto_increment
- `phone` varchar(20) not null
- `scene` enum('login','register','reset_password') not null
- `code_hash` char(64) not null
- `status` enum('sent','verified','expired','failed') not null
- `created_at` datetime not null
- `verified_at` datetime null

### 3.6 `password_reset_logs` 密码重置日志表

- `id` bigint PK auto_increment
- `user_id` bigint not null
- `phone` varchar(20) not null
- `reset_at` datetime not null
- `ip` varchar(64) null
- `user_agent` varchar(255) null

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

### 5.1 登录会话

- `auth:session:{token}` -> JSON `{phone, user_uuid}`，TTL=`30d`

### 5.2 验证码与冷却

- `auth:sms:{scene}:{phone}` -> `code_hash`，TTL=`300s`
- `auth:sms:cooldown:{scene}:{phone}` -> `1`，TTL=`60s`

### 5.3 忘记密码校验通过态

- `auth:pwdreset:verified:{phone}` -> `1`，TTL=`600s`

### 5.4 聊天上下文

- `RedisChatMessageHistory(session_id=user_uuid)` -> 最近 N 轮对话，TTL=`SESSION_TTL_SECONDS`
- `chat:preferred_name_prompt:{session_id}` -> 是否需要追问称呼偏好

### 5.5 特性开关与 probe

- `jiyi:feature_flags:v2` -> V2 / provider 开关
- `yuanfenju:merchant_probe:*` -> 缘分居额度与会员状态探测缓存

### 5.6 质量指标

- `jiyi:quality:metrics:{yyyymmdd}`
- `jiyi:quality:unique_output:{yyyymmdd}`
- `jiyi:quality:recent_output:{yyyymmdd}`

### 5.7 限流

- `ratelimit:sms:{phone}:{yyyyMMddHHmm}`
- `ratelimit:chat:{user_id}:{yyyyMMddHHmm}`

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
- 写资料采用：先写 MySQL，再刷新 Redis 会话视图
- 聊天消息当前主要保存在 Redis 短期上下文里
- merchant probe 与质量指标属于可重建缓存

---

## 8. 当前运维建议

1. MySQL 至少每日备份
2. Redis 选择合适的持久化策略
3. 发布前确认 `gender` 与 `preferred_name` 的资料读写无回归
4. 如果 provider 调整了额度探测或新增缓存键，及时同步 `03` 与 `06`
