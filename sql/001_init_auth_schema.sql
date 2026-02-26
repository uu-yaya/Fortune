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
