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
| `www.`, 루트, `corp.inazina.kr` | 4314 | 루트는 www 로 301 |
| `wl.kr` | 4315 | 자체 진입 비밀번호. SSO 대상 아님 |
| `work.inazina.kr` | (신규 업무 플랫폼) | Caddyfile 에 주석 자리만 있다 |
| `mc.inazina.kr` | 25565 TCP | Caddy 기본 기능 밖(`caddy-l4` 또는 DNS 직접 연결). 웹 SSO 아님 |
| 4313, `api.inazina.kr` | 공개하지 않음 | `/api/internal*` 도 앱 호스트에서 404 |

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
1. `.env` 에 `INAZINA_OIDC_ISSUER=https://id.inazina.localhost/oidc` 와 고정 `INAZINA_AUTH_SECRET` 을 넣는다.
2. `brew install caddy` 후 `./start_all.command` — Caddy 가 있고 위 값이 있으면 함께 시작한다(`INAZINA_TLS` 기본 `internal`).
3. Node 가 Caddy 의 로컬 인증서를 믿어야 앱이 IdP 와 통신한다: `caddy trust`(시스템 키체인에 등록, 관리자 암호 필요) 후, Node 용으로 `export NODE_EXTRA_CA_CERTS=<Caddy root.crt>` (경로는 `caddy environ` 의 데이터 폴더 아래 `pki/authorities/local/root.crt`).
4. 브라우저에서 `https://naru.inazina.localhost`. `*.localhost` 는 자동으로 이 컴퓨터를 가리킨다.
- 4310/4311 에 직접(`127.0.0.1`) 접속하면 이전처럼 비밀번호 로그인이 된다(앱 호스트가 아닌 접속은 SSO 를 강제하지 않는다). 운영자의 비상 경로이기도 하다.

## 디스코드 콜백 (배포 전 확인)
가입·비밀번호 재설정은 앱 호스트에서 디스코드 본인 확인을 한다. api 주소를 열지 않으므로 콜백을 앱 호스트로 옮겨야 한다.
- `.env`: `DISCORD_REDIRECT_URI=https://naru.inazina.kr/api/auth/discord/callback`
- **디스코드 개발자 포털**에도 같은 주소를 직접 추가해야 한다(코드로 할 수 없음). 둘이 다르면 가입·재설정만 조용히 실패하고 일반 로그인은 멀쩡해서 늦게 발견된다.
- 현재 `.env`·`.env.example`·코드 기본값은 아직 `http://127.0.0.1:4312/...` 이다(포털 등록 전에 바꾸면 로컬 가입이 깨진다).

## 동작
- 로그인: 앱 → `/api/auth/oidc/login` → IdP 로그인 화면 → `/api/auth/oidc/callback`(코드 교환, ID 토큰 서명·`iss`·`aud`·`nonce`·`auth_version` 검증) → 앱 세션 쿠키. 실패하면 `/?login_error=<코드>` 로 돌아오고 앱은 자동 재이동하지 않는다(무한 이동 방지).
- 세션은 12시간. 비밀번호를 바꾸거나 로그아웃하면 `authVersion` 이 올라가 **모든 앱의 세션이 끝난다**(기존 동작 그대로).
- 로그아웃: 앱 쿠키를 지우고 IdP 의 end-session 으로 이동해 SSO 세션도 끝낸다. 국가·회사 계정 대리 로그인(`$login`)은 앱 안에서만 처리되고 IdP 와 무관하다.
- 가입·비밀번호 재설정은 앱의 기존 디스코드 인증 화면에서 한다. IdP 로그인 화면의 링크가 `https://naru.<도메인>/?register=1`, `/?reset=1` 로 보낸다.
- 비상 비밀번호와 4312/4313 직접 접근 관문은 바뀌지 않았다.

## 확인된 것과 아직 확인하지 못한 것
- 확인: OIDC 흐름 전체(PKCE, 로그인, 코드 1회성, ID 토큰 서명·클레임, 로그아웃 후 재로그인, 변조 토큰 거부, 상태 불일치 거부)를 실제 provider 와 함께 자동 시험으로 확인했다(`npm run test:api`, `npm run test:shared`). Caddyfile 은 실제 Caddy(v2.11.9)로 문법과 라우팅(`id.` 의 경로 제한, `/api/internal*` 404, 리다이렉트)을 확인했다.
- 미확인: 실제 브라우저에서의 로그인 화면·리다이렉트, 실제 Python 데이터 서비스·DB 와 붙인 통합 실행(운영 DB 를 건드릴 수 있어 하지 않음), 운영 도메인에서의 Caddy 인증서 발급, Vite 개발 서버를 도메인 뒤에서 그대로 쓰는 경우의 호스트 허용(`server.allowedHosts`, 또는 Caddy 에서 `header_up Host`)은 배포 방식에 따라 필요할 수 있다.
