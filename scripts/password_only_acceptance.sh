#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://example.com}"
ACCOUNT="${ACCOUNT:-}"
PASSWORD="${PASSWORD:-}"
COOKIE_JAR="${COOKIE_JAR:-/tmp/jiyi-password-only.cookie}"

if [[ -z "$ACCOUNT" || -z "$PASSWORD" ]]; then
  echo "Usage: BASE_URL=https://your-domain ACCOUNT=JIYI-XXXX PASSWORD=xxxx $0"
  exit 2
fi

echo "[1/6] GET /login should be 200"
code=$(curl -sS -o /tmp/pw_login.out -w '%{http_code}' "$BASE_URL/login")
[[ "$code" == "200" ]] || { echo "FAILED: /login code=$code"; exit 1; }

echo "[2/6] POST /auth/login/password should be 200 and set cookie"
code=$(curl -sS -o /tmp/pw_auth.out -w '%{http_code}' -c "$COOKIE_JAR" \
  -X POST "$BASE_URL/auth/login/password" \
  -H 'Content-Type: application/json' \
  -d "{\"account\":\"$ACCOUNT\",\"password\":\"$PASSWORD\"}")
[[ "$code" == "200" ]] || { echo "FAILED: /auth/login/password code=$code"; cat /tmp/pw_auth.out; exit 1; }
grep -q "jiyi_auth_token" "$COOKIE_JAR" || { echo "FAILED: cookie not set"; exit 1; }

echo "[3/6] POST /chat should be 200 with cookie"
code=$(curl -sS -o /tmp/pw_chat.out -w '%{http_code}' -b "$COOKIE_JAR" \
  -X POST "$BASE_URL/chat" \
  -H 'Content-Type: application/json' \
  -d '{"query":"你好"}')
[[ "$code" == "200" ]] || { echo "FAILED: /chat code=$code"; cat /tmp/pw_chat.out; exit 1; }
grep -q "output" /tmp/pw_chat.out || { echo "FAILED: /chat no output field"; cat /tmp/pw_chat.out; exit 1; }

echo "[4/6] SMS endpoints should be blocked with 410"
for path in /auth/send_code /auth/verify /auth/password/verify_code /auth/password/reset; do
  code=$(curl -sS -o /tmp/pw_block.out -w '%{http_code}' \
    -X POST "$BASE_URL$path" \
    -H 'Content-Type: application/json' \
    -d '{}')
  [[ "$code" == "410" ]] || { echo "FAILED: $path expected 410 got $code"; cat /tmp/pw_block.out; exit 1; }
done

echo "[5/6] /register /forgot-password /reset-password should redirect to /login"
for path in /register /forgot-password /reset-password; do
  loc=$(curl -sS -I "$BASE_URL$path" | awk -F': ' '/^location:/ {print $2}' | tr -d '\r')
  [[ "$loc" == "/login" || "$loc" == "$BASE_URL/login" ]] || { echo "FAILED: $path redirect=$loc"; exit 1; }
done

echo "[6/6] unauthenticated /index should redirect to /login"
loc=$(curl -sS -I "$BASE_URL/index" | awk -F': ' '/^location:/ {print $2}' | tr -d '\r')
[[ "$loc" == "/login" || "$loc" == "$BASE_URL/login" ]] || { echo "FAILED: /index redirect=$loc"; exit 1; }

echo "PASS: password-only acceptance checks completed."
