# 통합 로그인(SSO)과 도메인 서버(Caddy)

2026-11-21 구축. 설계: `docs/superpowers/specs/2026-11-21-sso-design.md`, 계획: `docs/superpowers/plans/2026-11-21-sso-oidc.md`.

## 한눈에 보기
- **id.inazina.kr** 은 중앙 인증 서버(OIDC IdP)다. 로그인 화면과 토큰 발급만 한다. 앱(나루·콘솔)은 로그인이 필요할 때 이곳으로 보내고, 인증 코드 + PKCE(S256)로 돌려받아 **자기 도메인 전용 세션 쿠키**를 만든다.
- IdP는 별도 프로세스가 아니라 기존 Node API(4312) 안의 모듈이다(`apps/api/src/oidc/`). Caddy가 `id.` 호스트의 `/oidc/*`, `/login`, `/register`, `/reset` 만 4312로 넘기고 나머지는 404로 막는다.
- `INAZINA_OIDC_ISSUER` 를 **비우면 전부 이전 방식**(앱마다 비밀번호 로그인)으로 돌아간다. 문제가 생기면 이것이 되돌리는 방법이다.

| 호스트 | 대상 | 비고 |
|---|---|---|
| `id.inazina.kr` | 4312 (`/oidc/*`, `/login`, `/register`, `/reset` 만) | 통합 로그인 |
| `naru.inazina.kr` | 4311 (+ `/api/*` → 4312) | HTS. 비회원도 둘러볼 수 있다(하위 세션 작업) |
| `console.inazina.kr` | 4310 (+ `/api/*` → 4312) | 관리자 콘솔. 로그인은 최근 15분 이내여야 한다(`max_age`) |
| `www.`, 루트, `corp.inazina.kr` | 4314 (+ `/api/site/feedback` 만 4312) | 루트는 www 로 301. 문의·신고 폼만 API 를 부른다 |
| `wl.kr` | 4315 | 자체 진입 비밀번호. SSO 대상 아님 |
| `work.inazina.kr` | (신규 업무 플랫폼) | Caddyfile 에 주석 자리만 있다 |
| `mc.inazina.kr` | 25565 TCP | Caddy 기본 기능 밖(`caddy-l4` 또는 DNS 직접 연결). 웹 SSO 아님 |
| 4313, `api.inazina.kr` | 공개하지 않음 | `/api/internal*` 도 앱 호스트에서 404 |

**`api.inazina.kr` 이 필요 없는 이유:** 브라우저는 API 를 각 앱 호스트 안의 `/api/*` 로 부른다(`naru.`·`console.`, www 는 문의 폼 한 경로). 같은 도메인이라 CORS 도 필요 없고, 로그인은 `id.`, 디스코드 돌아오는 주소도 `id.` 에 있다. 앞으로 외부 시스템(예: 마인크래프트)이 API 를 불러야 하면 `api.` 전체를 열지 말고 필요한 경로 하나만 기존 호스트에 명시해서 연다.

## 환경 변수 (`.env`)
| 이름 | 뜻 |
|---|---|
| `INAZINA_OIDC_ISSUER` | 예: `https://id.inazina.kr/oidc`. 반드시 `/oidc` 로 끝난다. 비우면 SSO 꺼짐 |
| `INAZINA_DOMAIN` | 기본 `inazina.localhost`. 운영은 `inazina.kr` |
| `WL_DOMAIN` | 기본 `wl.kr.localhost`. 운영은 `wl.kr` |
| `INAZINA_TLS` | 로컬은 `internal`, 운영은 비움(공개 인증서 자동 발급) |
| `INAZINA_OIDC_CLIENTS` | (선택) 클라이언트 등록 JSON. 비우면 `naru`, `console` 이 `INAZINA_DOMAIN` 기준으로 자동 등록된다. 와일드카드·비-https(루프백 제외)는 거부 |
| `INAZINA_OIDC_JWKS` | (선택) 서명 키(JSON). 비우면 `database/oidc_jwks.json` 을 처음 한 번 만든다(권한 600, git 제외). 이 파일은 **비밀 키**이니 백업하고 공유하지 않는다 |
| `INAZINA_AUTH_SECRET` | **반드시 고정값**. IdP 쿠키 서명에도 쓴다. 바뀌면 모든 로그인이 풀린다 |
| `INAZINA_COOKIE_SECURE` | 운영은 `true` |
| `DISCORD_REDIRECT_URI` | 아래 "디스코드 콜백" 참고 |

## 로컬에서 켜기
1. `.env` 에 `INAZINA_OIDC_ISSUER=https://id.inazina.localhost/oidc` 와 고정 `INAZINA_AUTH_SECRET`(없으면 `openssl rand -hex 32` 로 만든 값)을 넣는다.
2. `brew install caddy` 후 `./start_all.command` — 위 값이 있고 Caddy 가 있으면 함께 시작한다(`INAZINA_TLS` 기본 `internal`). 로그인 화면(`apps/id`)이 없으면 처음 한 번 빌드한다.
3. **브라우저가 로컬 인증서를 믿게 하기:** `caddy trust` 는 **실행 중인 Caddy**(관리 주소 `localhost:2019`)에 물어보므로 Caddy 가 켜져 있어야 한다(`./start_all.command` 로 켰거나, 다른 터미널에서 `INAZINA_TLS=internal caddy run --config infra/Caddyfile --adapter caddyfile`). 그 상태에서 다른 터미널에 `caddy trust` 를 실행한다. 꺼져 있으면 `connection refused` 가 난다. Caddy 없이 하려면 `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt"`. macOS 가 관리자 암호(또는 Touch ID)를 물으면 입력한다. Caddy 가 만든 "Caddy Local Authority" 가 시스템에 신뢰 등록되어 Chrome·Safari 가 `https://naru.inazina.localhost` 를 경고 없이 연다(Firefox 는 별도 경고가 날 수 있다). 되돌리기: `caddy untrust`. 그 인증서의 `root.key` 는 이 맥에서 어떤 사이트 인증서든 만들 수 있으니 공유하지 않는다.
4. **API 서버(Node)가 로컬 인증서를 믿게 하기:** Node 는 키체인을 보지 않고 `NODE_EXTRA_CA_CERTS` 가 가리키는 파일만 믿는다. 개발 실행 스크립트는 이 값이 비어 있으면 시스템 인증서 묶음(`/etc/ssl/cert.pem`)을 쓰는데 직접 지정하면 그 기본값이 대체되므로, 두 파일을 합친다.
   ```
   cat /etc/ssl/cert.pem "$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt" > "$HOME/.inazina-ca.pem"
   ```
   그리고 `.env` 에 `NODE_EXTRA_CA_CERTS=/Users/<내 이름>/.inazina-ca.pem` 을 추가하고 `./start_all.command` 를 다시 시작한다(`caddy environ` 의 `caddy.AppDataDir` 가 Caddy 데이터 폴더다). 맥 업데이트로 `/etc/ssl/cert.pem` 이 바뀌면 `cat` 을 다시 실행한다. 이 설정은 앱 서버가 id 서버와 HTTPS 로 통신할 때만 필요하다.
5. 브라우저에서 `https://naru.inazina.localhost`. `*.localhost` 는 자동으로 이 컴퓨터를 가리킨다.
- 4310/4311 에 직접(`127.0.0.1`) 접속하면 이전처럼 비밀번호 로그인이 된다(앱 호스트가 아닌 접속은 SSO 를 강제하지 않는다). 운영자의 비상 경로이기도 하다.

## 운영 도메인 인증서 (Caddy 가 자동 발급)
Caddy 는 `INAZINA_TLS` 를 비우면(또는 지우면) 실제 도메인용 공개 인증서를 스스로 받고 갱신한다(Let's Encrypt/ZeroSSL). 받으려면 아래가 먼저 갖춰져야 한다. 하나라도 빠지면 발급이 실패한다.
1. **도메인과 DNS:** `inazina.kr` 이 등록·위임되어 있고 `inazina.kr`, `www.`, `corp.`, `id.`, `naru.`, `console.` 과 `wl.kr` 의 A(또는 AAAA) 레코드가 이 서버의 공인 IP 를 가리킨다. (2026-11-22 확인: `inazina.kr`·`wl.kr` 은 NS/SOA 도 조회되지 않았다. 도메인 등록·DNS 설정이 먼저다.)
2. **80/443 이 인터넷에서 닿는다:** 공유기 포트포워딩과 방화벽. 인증서 발급 때 인증 기관이 이 서버의 80/443 에 접속해 소유를 확인한다. 4310~4315 는 열지 않는다(`127.0.0.1` 에서만 듣는다).
3. **`.env`:** `INAZINA_DOMAIN=inazina.kr`, `WL_DOMAIN=wl.kr`, `INAZINA_TLS=`(빈 값), `INAZINA_COOKIE_SECURE=true`, `INAZINA_OIDC_ISSUER=https://id.inazina.kr/oidc`. 브라우저가 쓰는 앱 주소가 바뀌므로 `INAZINA_OIDC_CLIENTS` 를 따로 쓰고 있다면 그 안의 주소도 맞춘다.
4. Caddy 를 시작하고 로그에서 `certificate obtained successfully` 가 호스트마다 나오는지 본다. 실패하면 같은 로그에 이유(DNS 미해결, 80 접속 불가 등)가 나온다. 같은 도메인으로 짧은 시간에 실패를 반복하면 인증 기관의 횟수 제한에 걸리니, 원인을 고친 뒤 다시 시도한다.
- 설정 파일이 운영 모드에서 올바른지는 `INAZINA_DOMAIN=inazina.kr WL_DOMAIN=wl.kr caddy validate --config infra/Caddyfile --adapter caddyfile` 로 확인한다(시험 `tests/test_caddyfile.py` 가 로컬·운영 모드를 실제 Caddy 로 검증한다).

## 디스코드 (배포 전 확인)
가입·비밀번호 재설정·아이디 찾기는 **id 서버**가 디스코드로 본인 확인을 한다. 디스코드는 우리 서버로 신호를 보내지 않고, 인증이 끝나면 **사용자의 브라우저**를 등록된 주소로 돌려보낸다. 그래서 필요한 것은 사용자 브라우저가 열 수 있는 공개 주소이고 `api.` 주소일 필요는 없다.
- 돌아오는 주소는 `INAZINA_OIDC_ISSUER` 에서 자동으로 만들어진다: `<issuer 의 주소>/oidc/account/discord/callback` (운영 `https://id.inazina.kr/oidc/account/discord/callback`, 로컬 `https://id.inazina.localhost/oidc/account/discord/callback`). `.env` 에 따로 쓸 값은 없다(`DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` 은 그대로 쓴다).
- **디스코드 개발자 포털**(애플리케이션 → OAuth2 → Redirects)에 위 주소를 직접 추가하고 저장해야 한다(코드로 할 수 없음). 등록되지 않은 주소로는 디스코드가 돌려보내 주지 않는다. 포털과 실제 주소가 다르면 가입·재설정만 조용히 실패하고 일반 로그인은 멀쩡해서 늦게 발견된다.
- `.env` 의 `DISCORD_REDIRECT_URI` 는 SSO 를 끈 예전 방식(앱 안의 디스코드 화면)에서만 쓰인다. 그 방식은 돌아오는 주소가 `127.0.0.1:4310/4311` 로 코드에 박혀 있어 도메인 뒤에서는 동작하지 않는다.

## 동작
- 로그인: 앱 → `/api/auth/oidc/login` → id 서버의 로그인 화면 → `/api/auth/oidc/callback`(코드 교환, ID 토큰 서명·`iss`·`aud`·`nonce`·`auth_version` 검증) → 앱 세션 쿠키. 실패하면 `/?login_error=<코드>` 로 돌아오고 앱은 자동 재이동하지 않는다(무한 이동 방지).
- **화면:** id 서버는 대시보드와 같은 로그인 화면(`apps/shared/AuthPanel`, 3D 장면 포함)을 `apps/id` 로 빌드해 `/oidc/ui/` 아래에서 제공한다. 빌드가 없으면 서버가 그리는 간단한 로그인 페이지로 대신한다(가입 화면 없음). `npm run build --workspace @inazina/id`.
- **자동 로그인:** 옵션은 없다. 브라우저를 켜 둔 동안은 앱 사이를 오가도 로그인이 이어지고, 브라우저를 닫으면 풀린다(id 서버와 앱 세션 쿠키 모두 브라우저 세션 쿠키). 서명된 만료는 12시간이다.
- 세션은 12시간. 비밀번호를 바꾸거나 로그아웃하면 `authVersion` 이 올라가 **모든 앱의 세션이 끝난다**(기존 동작 그대로).
- 로그아웃: 앱 쿠키를 지우고 IdP 의 end-session 으로 이동해 SSO 세션도 끝낸다. 국가·회사 계정 대리 로그인(`$login`)은 앱 안에서만 처리되고 IdP 와 무관하다.
- 가입·비밀번호 재설정·아이디 찾기도 id 서버의 같은 화면에서 한다. 앱의 해당 버튼은 `/api/auth/oidc/login?mode=register|reset-password|find-id` 로 보내고, id 서버가 그 화면을 먼저 연다. 가입·재설정이 끝나면 그 계정으로 바로 로그인된다.
- 비상 비밀번호와 4312/4313 직접 접근 관문은 바뀌지 않았다.

## 확인된 것과 아직 확인하지 못한 것
- 확인: OIDC 흐름 전체(PKCE, 로그인, 코드 1회성, ID 토큰 서명·클레임, 로그아웃 후 재로그인, 변조 토큰 거부, 상태 불일치 거부)를 실제 provider 와 함께 자동 시험으로 확인했다(`npm run test:api`, `npm run test:shared`). Caddyfile 은 실제 Caddy(v2.11.9)로 문법과 라우팅(`id.` 의 경로 제한, `/api/internal*` 404, 리다이렉트)을 확인했다.
- 미확인: 실제 브라우저에서의 로그인 화면(`apps/id`)·리다이렉트·가입 화면 모양(빌드 결과가 서버에서 제공되는 것까지만 확인), 실제 Python 데이터 서비스·DB 와 붙인 통합 실행(운영 DB 를 건드릴 수 있어 하지 않음), 운영 도메인에서의 Caddy 인증서 발급, Vite 개발 서버를 도메인 뒤에서 그대로 쓰는 경우의 호스트 허용(`server.allowedHosts`, 또는 Caddy 에서 `header_up Host`)은 배포 방식에 따라 필요할 수 있다.
