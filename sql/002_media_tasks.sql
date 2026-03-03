CREATE TABLE IF NOT EXISTS media_tasks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id CHAR(32) NOT NULL UNIQUE,
  user_id BIGINT NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  scenario VARCHAR(64) NOT NULL,
  status ENUM('pending', 'running', 'succeeded', 'failed', 'timeout') NOT NULL DEFAULT 'pending',
  dify_run_id VARCHAR(128) NULL,
  input_json JSON NULL,
  output_json JSON NULL,
  error_code VARCHAR(64) NULL,
  error_message VARCHAR(255) NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  INDEX idx_media_user_created (user_id, created_at),
  INDEX idx_media_session_created (session_id, created_at),
  INDEX idx_media_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
