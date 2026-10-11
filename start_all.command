#!/bin/zsh

set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi

if [[ ! -x "$PYTHON" ]]; then
  print -u2 "Python 3를 찾을 수 없습니다. Python 환경을 먼저 준비해주세요."
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  print -u2 "npm을 찾을 수 없습니다. Node.js 설치 상태를 확인해주세요."
  exit 1
fi
if [[ ! -d "$ROOT/node_modules" ]]; then
  print -u2 "의존성이 없습니다. 프로젝트 폴더에서 npm install을 먼저 실행해주세요."
  exit 1
fi

if [[ -z "${INAZINA_TOKEN:-${NACONOMICS_TOKEN:-}}" ]]; then
  print -u2 "주의: .env에 INAZINA_TOKEN이 없어 Discord 봇은 시작되지 않습니다."
  print -u2 "프로젝트 루트 .env에 INAZINA_TOKEN=봇_토큰 을 추가한 뒤 다시 실행해주세요."
fi

typeset -a pids
pids=()

cleanup() {
  trap - INT TERM EXIT
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in $pids; do
    wait "$pid" 2>/dev/null || true
  done
}

trap cleanup INT TERM EXIT

print "INAZINA 서비스를 시작합니다. 종료하려면 Ctrl+C를 누르세요."
print "Dashboard: http://127.0.0.1:4310"
print "HTS:       http://127.0.0.1:4311"
print "API:       http://127.0.0.1:4312/api/health"
print "로그는 아래에 서비스별로 표시됩니다."

if lsof -nP -iTCP:4312 -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iTCP:4313 -sTCP:LISTEN >/dev/null 2>&1; then
  print "API 또는 Python 데이터 서비스가 이미 실행 중이라 건너뜁니다."
else
  npm run dev:api &
  pids+=("$!")
fi

if lsof -nP -iTCP:4310 -sTCP:LISTEN >/dev/null 2>&1; then
  print "Dashboard가 이미 실행 중이라 건너뜁니다."
else
  npm run dev:dashboard &
  pids+=("$!")
fi

if lsof -nP -iTCP:4311 -sTCP:LISTEN >/dev/null 2>&1; then
  print "HTS가 이미 실행 중이라 건너뜁니다."
else
  npm run dev:hts &
  pids+=("$!")
fi

if pgrep -f "$ROOT/run.py" >/dev/null 2>&1; then
  print "Discord 봇이 이미 실행 중이라 건너뜁니다."
else
  "$PYTHON" "$ROOT/run.py" &
  pids+=("$!")
fi

wait
