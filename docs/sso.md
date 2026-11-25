# 통합 로그인(SSO)과 도메인 서버(Caddy)

설계: `docs/superpowers/specs/2026-11-21-sso-design.md`, 계획: `docs/superpowers/plans/2026-11-21-sso-oidc.md` (2026-11-22 결정으로 아래가 이 둘보다 우선한다).

## 한눈에 보기
- **로그인은 id 서버 하나뿐이다.** 나루(`naru.`)와 콘솔(`console.`)에는 독립 로그인이 없다. 로그인·가입·아이디 찾기·비밀번호 재설정은 모두 `id.inazina.localhost` 의 같은 화면(대시보드 로그인 화면)에서 한다. 앱은 로그인이 필요할 때 id 서버로 보내고, 인증 코드 + PKCE(S256)로 돌려받아 **자기 도메인 전용 세션 쿠키**를 만든다.
- id 서버(OIDC IdP)는 별도 프로세스가 아니라 기존 Node API(4312) 안의 모듈이다(`apps/api/src/oidc/`). 로그인 화면은 `apps/id` 를 빌드한 것을 `/oidc/ui/` 아래에서 제공한다.
- 이 서비스는 **이 맥 한 대 안에서, 내부망 전용**으로 쓴다. 공개 DNS도 공개 인증서도 없다. 인증서는 Caddy 가 직접 만드는 로컬 인증서(`tls internal`)다. (`.kr` 도메인 계획은 폐기했다.)

## 도메인 (지금 쓰는 전부)
| 이름 | 가는 곳 | 비고 |
|---|---|---|
| `id.inazina.localhost` | 4312 (`/oidc/*`, `/login`, `/register`, `/reset` 만) | 통합 로그인. 나머지 경로(로고의 `/` 포함)는 공개 사이트로 302 |
| `naru.inazina.localhost` | 4311 + `/api/*` → 4312 | HTS. 비회원도 둘러볼 수 있다 |
| `console.inazina.localhost` | 4310 + `/api/*` → 4312 | 관리자 콘솔. 로그인은 최근 15분 이내여야 한다(`max_age`) |
| `inazina.localhost`, `www.`, `corp.` | (서비스 없음) | 공식 사이트는 GitHub Pages(`https://winterliy.github.io/INAZINA/`)로 옮겼다. 이 이름들은 그곳으로 302 리디렉션만 한다(`INAZINA_PUBLIC_SITE`). 4314는 더 이상 실행하지 않는다 |
| `inazina.localhost` | `www.` 로 301 | |
| `wl.localhost` | 4315 | 위키 뷰어. **이 이름 하나만** 쓴다. SSO 대상이 아니고 자체 진입 비밀번호가 있다. `INAZINA_WL=off` 로 끈다 |
| `api.…` , 4313 | **공개하지 않음** | 앱은 API 를 자기 호스트의 `/api/*` 로 부른다. `/api/internal*` 은 404 |
| `work.`, `mc.` | **보류** | Caddyfile 에 자리만 있다(주석) |

`*.localhost` 는 이 맥에서 자동으로 자기 자신(127.0.0.1)으로 풀린다. Safari 를 위해 `/etc/hosts` 에도 같은 이름들을 넣어 두었다(`127.0.0.1 id.inazina.localhost naru.… console.… www.… corp.… wl.localhost inazina.localhost`).

## 동작
- 로그인: 앱 → `/api/auth/oidc/login` → id 서버 로그인 화면 → `/api/auth/oidc/callback`(코드 교환, ID 토큰 서명·`iss`·`aud`·`nonce`·`auth_version` 검증) → 앱 세션 쿠키. 실패하면 `/?login_error=<코드>` 로 돌아오고 앱은 자동 재이동하지 않는다(무한 이동 방지).
- 가입·재설정·아이디 찾기: 앱의 해당 요청은 `/api/auth/oidc/login?mode=register|reset-password|find-id` 로 가고 id 서버가 그 화면을 먼저 연다. 가입·재설정이 끝나면 그 계정으로 바로 로그인된다.
- 나루는 비회원도 첫 화면·시세·차트·히트맵을 볼 수 있다. 로그인이 필요한 부분은 흐리게 보이고 클릭하면 id 서버로 간다(개인 페이지는 바로 이동). 콘솔은 로그인 없이는 바로 id 서버로 간다.
- **자동 로그인:** 옵션은 없다. 브라우저를 켜 둔 동안은 앱 사이를 오가도 로그인이 이어지고, 브라우저를 닫으면 풀린다(id 서버와 앱 세션 쿠키 모두 브라우저 세션 쿠키). 서명된 만료는 12시간이다.
- 세션은 비밀번호를 바꾸거나 로그아웃하면 `authVersion` 이 올라가 **모든 앱의 세션이 끝난다**. 로그아웃은 앱 쿠키를 지우고 id 서버의 end-session 으로 이동해 SSO 세션도 끝낸다. 국가·회사 계정 대리 로그인(`$login`)은 앱 안에서만 처리된다.
- **4312·4313 의 개별 로그인(브라우저 접근 관문, 비상 비밀번호, 접속 시도 기록, 안내 화면)도 삭제했다.** 브라우저로 열면 JSON 이 그대로 보이고, 로그인이 필요한 응답은 JSON 401 이다(4313 은 내부 토큰이 없으면 어떤 경로든 JSON 401). 앱의 옛 로그인 경로(`/api/auth/login`, `/register`, `/password/reset`, 디스코드 경로)도 **삭제**했다. `127.0.0.1:4310` 같은 직접 접속으로는 로그인할 수 없다(등록된 앱 주소로만 로그인된다).

## 환경 변수 (`.env`, 모두 선택)
| 이름 | 뜻 |
|---|---|
| `INAZINA_OIDC_ISSUER` | 기본 `https://id.<INAZINA_DOMAIN>/oidc`. 바꿀 때는 반드시 `/oidc` 로 끝난다 |
| `INAZINA_DOMAIN` | 기본 `inazina.localhost` |
| `INAZINA_FRONTEND` | `dev`(기본: Vite 개발 서버) 또는 `static`(화면을 빌드해 Caddy 가 직접 제공) |
| `INAZINA_WL` | `off` 면 위키 뷰어(`wl.localhost`)를 켜지 않는다 |
| `INAZINA_AUTH_SECRET` | **고정값**. 로그인 쿠키 서명에 쓴다. 바뀌면 모든 로그인이 풀린다 |
| `INAZINA_OIDC_CLIENTS` | 클라이언트 등록 JSON. 비우면 `naru`, `console` 이 자동 등록된다 |
| `INAZINA_OIDC_JWKS` | 서명 키(JSON). 비우면 `database/oidc_jwks.json` 을 처음 한 번 만든다(권한 600, git 제외, **비밀 키**: 백업하고 공유하지 않는다) |
| `INAZINA_COOKIE_SECURE` | 앱이 https 로만 열리므로 `true` 를 권장한다 |
| `INAZINA_GUEST_RATE_PER_MINUTE` | 비회원 시세 요청 제한(주소당 분당). 기본 240 |
| `NODE_EXTRA_CA_CERTS` | Node 가 Caddy 로컬 인증서를 믿게 하는 인증서 묶음 파일 경로 |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | 디스코드 본인 확인 |

## 켜기 (이 맥)
1. `brew install caddy`. `./start_all.command` 가 Caddy 를 함께 시작한다(처음에는 로그인 화면 `apps/id` 를 빌드한다).
2. **브라우저가 Caddy 인증서를 믿게 하기:** `caddy trust` 는 **실행 중인 Caddy**(관리 주소 `localhost:2019`)에 물어보므로 Caddy 가 켜져 있어야 한다(`./start_all.command` 로 켰거나, 다른 터미널에서 `INAZINA_FRONTEND=dev caddy run --config infra/Caddyfile --adapter caddyfile`). 그 상태에서 다른 터미널에 `caddy trust` 를 실행한다. 꺼져 있으면 `connection refused` 가 난다. Caddy 없이 하려면 `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt"`. 되돌리기: `caddy untrust`. 그 인증서의 `root.key` 는 이 맥에서 어떤 사이트 인증서든 만들 수 있으니 공유하지 않는다.
3. **API 서버(Node)가 Caddy 인증서를 믿게 하기:** Node 는 키체인을 보지 않고 `NODE_EXTRA_CA_CERTS` 가 가리키는 파일만 믿는다. 개발 실행 스크립트는 이 값이 비어 있으면 시스템 인증서 묶음(`/etc/ssl/cert.pem`)을 쓰는데 직접 지정하면 그 기본값이 대체되므로 두 파일을 합친다.
   ```
   cat /etc/ssl/cert.pem "$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt" > "$HOME/.inazina-ca.pem"
   ```
   `.env` 에 `NODE_EXTRA_CA_CERTS=/Users/<내 이름>/.inazina-ca.pem` 을 넣고 `./start_all.command` 를 다시 시작한다. 맥 업데이트로 `/etc/ssl/cert.pem` 이 바뀌면 `cat` 을 다시 실행한다.
4. Safari 는 `/etc/hosts` 항목이 있어야 이름이 풀린다(위 도메인 절). 항목을 고치면 `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`.
5. 브라우저에서 `https://naru.inazina.localhost`.
- Caddy 설정만 다시 읽히려면(서비스 재시작 없이): `INAZINA_FRONTEND=dev INAZINA_ROOT="$PWD" caddy reload --config infra/Caddyfile --adapter caddyfile`.

## 디스코드
가입·비밀번호 재설정·아이디 찾기는 id 서버가 디스코드로 본인 확인을 한다. 디스코드는 우리 서버로 신호를 보내지 않고, 인증이 끝나면 **사용자의 브라우저**를 등록된 주소로 돌려보낸다.
- 돌아오는 주소는 `INAZINA_OIDC_ISSUER` 에서 자동으로 만들어진다: **`https://id.inazina.localhost/oidc/account/discord/callback`**. `.env` 에 따로 쓸 값은 없다.
- **디스코드 개발자 포털**(애플리케이션 → OAuth2 → Redirects)에 위 주소를 직접 추가하고 저장해야 한다(코드로 할 수 없음). 등록되지 않은 주소(예전에 쓰던 `.kr` 주소 등)면 디스코드가 **"Invalid OAuth2 redirect_uri"** 오류를 보여 준다. 오류가 나면 이 주소와 포털에 등록된 주소가 글자 그대로 같은지 먼저 본다.

## 화면 제공 방식 (`INAZINA_FRONTEND`)
기본(`dev`)은 `./start_all.command` 가 화면(나루, 대시보드, 공식 사이트)을 **Vite 개발 서버**로 켜고 Caddy 가 그 앞에서 이름만 연결한다. 개발 서버는 작업용이라 코드를 원본에 가깝게 내보내고 최적화가 없다. `INAZINA_FRONTEND=static` 을 넣으면 시작할 때 빌드가 없거나 소스보다 오래된 `hts`, `dashboard`, `site`, `id` 를 `npm run build` 로 만들고, **Caddy 가 `apps/<앱>/dist` 를 직접 제공한다**(`/api/*` 만 4312 로). 실제 Caddy 로 확인한 것: 클라이언트 경로(`/account` 등)는 앱으로 대체되고, 해시 파일은 1년 캐시·gzip, `index.html` 은 `no-cache`, `/src/main.tsx`·`/@vite/client`·`/.env`·`/package.json`·`../` 는 원본이 나오지 않는다. 위키 뷰어는 서버 기능이 Vite 안에만 있어 정적으로 만들 수 없으므로 항상 Vite 개발 서버를 프록시한다.

## 남은 결정·한계
- **API 실행 방식(결정 2026-11-22): 빌드 방식.** `start_all` 이 시작할 때 현재 작업 폴더의 코드로 API 를 빌드(`apps/api/dist`, 없거나 소스가 더 새로우면)해서 `node dist/index.js` 로 돌린다. 소스를 고쳐도 자동으로 다시 시작되지 않으니 `start_all` 을 다시 시작해야 반영된다. 별도 운영용 폴더 없이 **지금 작업 폴더의 코드를 그대로 실행**한다. (빌드본을 실제로 띄우면 데이터 서비스가 운영 DB 에 붙으므로 여기서는 빌드와 구문 검사까지만 했다.) 터미널을 닫아도 계속 돌게 할지(`launchd`)는 아직 정하지 않았다.
- `work.`(신규 업무 플랫폼), `mc.`(마인크래프트 TCP 25565: Caddy 기본 기능 밖, `caddy-l4` 가 필요)는 보류.
- 비밀번호 변경·로그아웃은 모든 앱 세션을 끊지만, 다른 곳에 남은 id 서버 세션(최대 12시간, 브라우저를 닫으면 사라짐)은 조용히 다시 로그인시킬 수 있다. 콘솔은 15분 규칙으로 보완된다.

## 확인된 것과 아직 확인하지 못한 것
- 확인: OIDC 흐름 전체(PKCE, 로그인, 코드 1회성, ID 토큰 서명·클레임, 로그아웃 후 재로그인, 변조 토큰 거부, 상태 불일치 거부)를 실제 provider 와 함께 자동 시험으로 확인했다(`npm run test:api`, `npm run test:shared`, `npm run test:hts`, `tests/test_caddyfile.py`). 실제 Chrome(DevTools 프로토콜)으로 나루 비회원 화면, 흐림 영역 클릭 → 로그인, `/account` 이동, 가입·재설정 화면, 콘솔 자동 이동, 잘못된 아이디의 로그인 실패 문구를 확인했다.
- 미확인: 실제 로그인 성공·디스코드 인증을 끝까지 하는 흐름(계정·디스코드 로그인이 필요), 브라우저를 닫았다 열 때 로그인이 풀리는지, Safari 에서의 접속, 실제 Python 서비스·DB 와 붙인 통합 실행(운영 DB 를 건드릴 수 있어 하지 않음).
