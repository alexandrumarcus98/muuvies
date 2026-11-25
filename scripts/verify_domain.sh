#!/usr/bin/env bash
set -euo pipefail

TARGET_DOMAIN="${1:-${DOMAIN:-}}"
EXPECTED_IP="${2:-${EXPECTED_IP:-}}"

if [ -z "${TARGET_DOMAIN}" ]; then
    echo "Usage: $0 <domain> [expected-ip]" >&2
    exit 2
fi

echo "=========================================================="
echo "  muuvies Domain & TLS Verification: ${TARGET_DOMAIN}"
echo "=========================================================="

echo -n "[1/5] Checking DNS A record resolution... "
RESOLVED_IP=$(python3 -c "import socket, sys; print(socket.gethostbyname(sys.argv[1]))" "${TARGET_DOMAIN}" 2>/dev/null || echo "NOT_RESOLVED")

if [ "${RESOLVED_IP}" = "NOT_RESOLVED" ]; then
    echo "WAITING"
    echo "      DNS could not resolve ${TARGET_DOMAIN}."
    exit 1
elif [ -n "${EXPECTED_IP}" ] && [ "${RESOLVED_IP}" != "${EXPECTED_IP}" ]; then
    echo "MISMATCH"
    echo "      Resolved to ${RESOLVED_IP}, expected ${EXPECTED_IP}."
    exit 1
else
    echo "OK (${RESOLVED_IP})"
fi

echo -n "[2/5] Testing HTTP port 80 connectivity... "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://${TARGET_DOMAIN}/api/health" || echo "FAILED")
echo "Status: ${HTTP_CODE} (Redirect to HTTPS or OK)"

echo -n "[3/5] Testing HTTPS port 443 TLS handshake... "
HTTPS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://${TARGET_DOMAIN}/api/health" || echo "FAILED")
echo "Status: ${HTTPS_CODE}"

echo -n "[4/5] Verifying reverse proxy to FastAPI... "
HEALTH_JSON=$(curl -fsS "https://${TARGET_DOMAIN}/api/health" 2>/dev/null || true)
if echo "${HEALTH_JSON}" | grep -q '"status":"healthy"'; then
    echo "SUCCESS!"
else
    echo "PENDING / UNHEALTHY"
    exit 1
fi

echo -n "[5/5] Verifying /static/app/* immutable caching... "
PAGE_HTML=$(curl -fsS "https://${TARGET_DOMAIN}/" 2>/dev/null || true)
ASSET_PATH=$(echo "${PAGE_HTML}" | grep -oE '/static/app/assets/[A-Za-z0-9._-]+\.js' | head -n1 || true)
if [ -z "${ASSET_PATH}" ]; then
    echo "FAILED (no /static/app/assets/*.js script found on landing page)"
    exit 1
fi
STATIC_HEADERS=$(curl -sS -D - -o /dev/null "https://${TARGET_DOMAIN}${ASSET_PATH}" 2>/dev/null || true)
STATIC_CODE=$(echo "${STATIC_HEADERS}" | awk 'NR==1 {print $2}')
if [ "${STATIC_CODE}" = "200" ] && echo "${STATIC_HEADERS}" | grep -qiE "cache-control:.*public.*max-age=31536000.*immutable"; then
    echo "OK (${ASSET_PATH} -> 200, immutable)"
    echo "      Service is live and secure at https://${TARGET_DOMAIN}"
else
    echo "FAILED (status=${STATIC_CODE:-NONE}, asset=${ASSET_PATH})"
    exit 1
fi
