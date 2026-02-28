#!/usr/bin/env bash
set -u

ROOT_DIR="${ROOT_DIR:-/opt/fortune-telling}"
APP_LOCAL_URL="${APP_LOCAL_URL:-http://127.0.0.1:8001}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://47.86.18.234}"
ENABLE_BACKUP="${ENABLE_BACKUP:-1}"

TS="$(date +%F_%H%M%S)"
REPORT_DIR="${ROOT_DIR}/output/ops"
REPORT_FILE="${REPORT_DIR}/daily_check_${TS}.log"

PASS=0
WARN=0
FAIL=0

mkdir -p "$REPORT_DIR"

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$REPORT_FILE"
}
pass() { PASS=$((PASS+1)); log "PASS: $*"; }
warn() { WARN=$((WARN+1)); log "WARN: $*"; }
fail() { FAIL=$((FAIL+1)); log "FAIL: $*"; }

check_http_code() {
  local url="$1"
  local expect="$2"
  local code
  code="$(curl -sS -o /dev/null -w "%{http_code}" "$url" || true)"
  if [[ "$code" == "$expect" ]]; then
    pass "$url -> $code"
  else
    fail "$url -> $code (expect $expect)"
  fi
}

cd "$ROOT_DIR" || { echo "cannot cd $ROOT_DIR"; exit 2; }
log "=== Daily Check Start ==="
log "ROOT_DIR=$ROOT_DIR"

log "[1] docker compose ps"
docker compose ps | tee -a "$REPORT_FILE"

# 容器状态
for svc in numerology redis mysql; do
  cid="$(docker compose ps -q "$svc" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    fail "service $svc not found"
    continue
  fi
  status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || true)"
  if [[ "$status" == "running" ]]; then
    pass "service $svc running"
  else
    fail "service $svc status=$status"
  fi
done

# mysql 健康状态
mysql_cid="$(docker compose ps -q mysql 2>/dev/null || true)"
if [[ -n "$mysql_cid" ]]; then
  mysql_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$mysql_cid" 2>/dev/null || true)"
  if [[ "$mysql_health" == "healthy" ]]; then
    pass "mysql health=healthy"
  else
    fail "mysql health=$mysql_health"
  fi
fi

# 应用本机健康
log "[2] local http check"
check_http_code "${APP_LOCAL_URL}/login" "200"
check_http_code "${APP_LOCAL_URL}/docs" "200"

# 公网入口检查
log "[3] public entry check"
pub_login_code="$(curl -sS -o /dev/null -w "%{http_code}" "${PUBLIC_BASE_URL}/login" || true)"
if [[ "$pub_login_code" =~ ^(200|301|302)$ ]]; then
  pass "${PUBLIC_BASE_URL}/login -> ${pub_login_code}"
else
  warn "${PUBLIC_BASE_URL}/login -> ${pub_login_code}"
fi

# 应用日志错误扫描
log "[4] numerology logs scan"
num_err="$(docker compose logs --tail=200 numerology 2>/dev/null | grep -Ein 'ERROR|Traceback|Exception|服务处理异常|connection refused|timeout' || true)"
if [[ -n "$num_err" ]]; then
  warn "numerology logs contain error keywords"
  echo "$num_err" | tee -a "$REPORT_FILE"
else
  pass "numerology logs no obvious errors"
fi

# Nginx 错误日志扫描
log "[5] nginx error log scan"
if [[ -f /var/log/nginx/error.log ]]; then
  ngx_err="$(tail -n 200 /var/log/nginx/error.log | grep -Ein 'error|crit|emerg|failed' || true)"
  if [[ -n "$ngx_err" ]]; then
    warn "nginx error.log has error keywords"
    echo "$ngx_err" | tee -a "$REPORT_FILE"
  else
    pass "nginx error.log clean (last 200 lines)"
  fi
else
  warn "/var/log/nginx/error.log not found"
fi

# 读取 MySQL root 密码
MYSQL_ROOT_PASSWORD="$(grep '^MYSQL_ROOT_PASSWORD=' .env 2>/dev/null | cut -d= -f2- || true)"
if [[ -z "$MYSQL_ROOT_PASSWORD" ]]; then
  fail "MYSQL_ROOT_PASSWORD not found in .env"
else
  log "[6] mysql query check"
  if docker compose exec -T -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql \
    mysql -uroot -D fortune_telling -e "SELECT NOW() AS now_time, COUNT(*) AS users FROM users;" \
    >>"$REPORT_FILE" 2>&1; then
    pass "mysql query ok"
  else
    fail "mysql query failed"
  fi
fi

# Redis 检查
log "[7] redis check"
redis_ping="$(docker compose exec -T redis redis-cli PING 2>/dev/null | tr -d '\r' || true)"
if [[ "$redis_ping" == "PONG" ]]; then
  pass "redis ping=PONG"
else
  fail "redis ping=$redis_ping"
fi

redis_dbsize="$(docker compose exec -T redis redis-cli DBSIZE 2>/dev/null | tr -d '\r' || true)"
if [[ "$redis_dbsize" =~ ^[0-9]+$ ]]; then
  pass "redis dbsize=$redis_dbsize"
else
  fail "redis dbsize invalid: $redis_dbsize"
fi

# 资源余量
log "[8] disk/memory check"
root_use="$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')"
if [[ "$root_use" =~ ^[0-9]+$ ]]; then
  if (( root_use < 80 )); then
    pass "disk usage / = ${root_use}%"
  else
    warn "disk usage / = ${root_use}% (>=80%)"
  fi
else
  warn "cannot parse disk usage"
fi
free -h | tee -a "$REPORT_FILE"

# password-only 策略检查
log "[9] password-only policy check"
send_code_status="$(curl -sS -o /dev/null -w "%{http_code}" -X POST "${PUBLIC_BASE_URL}/auth/send_code" -H 'Content-Type: application/json' -d '{}' || true)"
if [[ "$send_code_status" == "410" ]]; then
  pass "/auth/send_code blocked (410)"
else
  warn "/auth/send_code status=$send_code_status (expect 410)"
fi

register_status="$(curl -sS -o /dev/null -w "%{http_code}" "${PUBLIC_BASE_URL}/register" || true)"
if [[ "$register_status" =~ ^(301|302)$ ]]; then
  pass "/register redirects (${register_status})"
else
  warn "/register status=$register_status (expect 301/302)"
fi

# 备份
if [[ "$ENABLE_BACKUP" == "1" && -n "$MYSQL_ROOT_PASSWORD" ]]; then
  log "[10] mysql backup"
  mkdir -p /opt/backups
  BACKUP_FILE="/opt/backups/fortune_telling_${TS}.sql"
  if docker compose exec -T -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql \
    mysqldump -uroot fortune_telling > "$BACKUP_FILE" 2>>"$REPORT_FILE"; then
    pass "backup created: $BACKUP_FILE"
  else
    fail "backup failed"
  fi
else
  warn "backup skipped (ENABLE_BACKUP=$ENABLE_BACKUP)"
fi

log "=== Summary: PASS=$PASS WARN=$WARN FAIL=$FAIL ==="
log "Report: $REPORT_FILE"

if (( FAIL > 0 )); then
  exit 1
fi
exit 0
