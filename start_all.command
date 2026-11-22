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

# INAZINA_FRONTEND=static (for the internet, docs/sso.md): the screens are built once and served by Caddy itself, with no Vite
# development servers for visitors. Default dev: Vite development servers, as always.
FRONTEND="${INAZINA_FRONTEND:-dev}"
if [[ "$FRONTEND" != "dev" && "$FRONTEND" != "static" ]]; then
  print -u2 "INAZINA_FRONTEND 는 dev 또는 static 이어야 합니다(지금 값: $FRONTEND)."
  exit 1
fi

print "INAZINA 서비스를 시작합니다. 종료하려면 Ctrl+C를 누르세요."
if [[ "$FRONTEND" == "static" ]]; then
  print "화면 모드: static (빌드된 파일을 Caddy 가 직접 제공, Vite 개발 서버 없음)"
else
  print "Dashboard: http://127.0.0.1:4310"
  print "HTS:       http://127.0.0.1:4311"
  print "Site:      http://127.0.0.1:4314"
fi
print "API:       http://127.0.0.1:4312/api/health"
print "WL:        http://127.0.0.1:4315"
print "로그는 아래에 서비스별로 표시됩니다."

if lsof -nP -iTCP:4310 -sTCP:LISTEN >/dev/null 2>&1 \
  || lsof -nP -iTCP:4311 -sTCP:LISTEN >/dev/null 2>&1 \
  || lsof -nP -iTCP:4312 -sTCP:LISTEN >/dev/null 2>&1 \
  || lsof -nP -iTCP:4313 -sTCP:LISTEN >/dev/null 2>&1 \
  || lsof -nP -iTCP:4314 -sTCP:LISTEN >/dev/null 2>&1 \
  || lsof -nP -iTCP:4315 -sTCP:LISTEN >/dev/null 2>&1 \
  || pgrep -f "$ROOT/run.py" >/dev/null 2>&1; then
  print -u2 "기존 INAZINA 서비스가 실행 중입니다."
  print -u2 "이전 실행 터미널에서 Ctrl+C로 전체 종료한 뒤 다시 실행해주세요."
  exit 1
fi

# The admin login map (DB-IP City Lite) is refreshed monthly; a failed download keeps the old file.
GEOIP="$ROOT/database/geoip/dbip-city-lite.mmdb"
if [[ ! -f "$GEOIP" ]] || [[ -n "$(find "$GEOIP" -mtime +35 2>/dev/null)" ]]; then
  "$PYTHON" "$ROOT/tools/update_geoip.py" >/dev/null 2>&1 &
fi

# Single sign-on: the identity provider serves the login screen built from apps/id (built once; the API falls back to a plain page without it).
if [[ -n "${INAZINA_OIDC_ISSUER:-}" && ! -f "$ROOT/apps/id/dist/index.html" ]]; then
  print "통합 로그인 화면(apps/id)을 빌드합니다."
  npm run build --workspace @inazina/id >/dev/null 2>&1 || print -u2 "주의: 로그인 화면 빌드에 실패해 간단한 로그인 페이지로 대신합니다. (npm run build --workspace @inazina/id 로 원인을 확인하세요.)"
fi

# Static mode: build the screens whose build is missing or older than their sources, and refuse to start without Caddy
# (it is the only thing that serves them).
if [[ "$FRONTEND" == "static" ]]; then
  if ! command -v caddy >/dev/null 2>&1; then
    print -u2 "INAZINA_FRONTEND=static 은 화면을 Caddy 가 제공하므로 caddy 가 필요합니다. (brew install caddy)"
    exit 1
  fi
  rebuild=0
  for app in hts dashboard site id; do
    built="$ROOT/apps/$app/dist/index.html"
    if [[ ! -f "$built" ]] || [[ -n "$(find "$ROOT/apps/$app/src" "$ROOT/apps/$app/index.html" "$ROOT/apps/shared" -newer "$built" -print -quit 2>/dev/null)" ]]; then rebuild=1; fi
  done
  if (( rebuild )); then
    print "화면을 빌드합니다(hts, dashboard, site, id)."
    npm run build --workspace @inazina/hts --workspace @inazina/dashboard --workspace @inazina/site --workspace @inazina/id >/dev/null 2>&1 || {
      print -u2 "화면 빌드에 실패했습니다. 원인을 확인하세요: npm run build --workspace @inazina/hts (dashboard, site, id 도 같은 방식)"
      exit 1
    }
  fi
fi

npm run dev:api &
pids+=("$!")
if [[ "$FRONTEND" != "static" ]]; then
  npm run dev:dashboard &
  pids+=("$!")
  npm run dev:hts &
  pids+=("$!")
  npm run dev:site &
  pids+=("$!")
fi
# The wiki viewer's server part exists only in Vite; Caddy serves it as wl.<domain> (INAZINA_WL=off turns that off).
npm run dev:wl &
pids+=("$!")

# The front door (docs/sso.md): Caddy, when single sign-on is on (INAZINA_OIDC_ISSUER) or the screens are static.
if [[ -n "${INAZINA_OIDC_ISSUER:-}" || "$FRONTEND" == "static" ]]; then
  if command -v caddy >/dev/null 2>&1; then
    INAZINA_FRONTEND="$FRONTEND" INAZINA_ROOT="$ROOT" caddy run --config "$ROOT/infra/Caddyfile" --adapter caddyfile &
    pids+=("$!")
  else
    print -u2 "주의: INAZINA_OIDC_ISSUER 가 설정됐지만 caddy 가 설치돼 있지 않아 도메인 서버(infra/Caddyfile)를 시작하지 않습니다."
  fi
fi

# The API owns the Python data service, which initializes and migrates the
# shared SQLite database. Start the Discord bot only after that initialization
# is complete so both processes do not contend for startup DDL/writes.
print "Python 데이터 서비스와 DB 초기화를 기다립니다."
python_ready=0
for attempt in {1..90}; do
  health="$(curl -fsS --max-time 2 http://127.0.0.1:4312/api/health 2>/dev/null || true)"
  if [[ "$health" == *'"python":"connected"'* ]]; then
    python_ready=1
    break
  fi
  sleep 1
done
if (( ! python_ready )); then
  print -u2 "Python 데이터 서비스가 90초 안에 준비되지 않아 전체 실행을 중단합니다. 위의 API 로그를 확인해주세요."
  exit 1
fi
print "Python 데이터 서비스와 DB가 준비됐습니다. Discord 봇을 시작합니다."
"$PYTHON" "$ROOT/run.py" &
pids+=("$!")

wait
