# INAZINA

디스코드 서버에서 운영하는 가상 국가·금융 경제 시뮬레이터입니다. 국가별 통화와 국고, 은행·대출, 주식·ETF·옵션·선물·ELS·채권·펀드 거래, 복권과 게임을 한 SQLite DB 위에서 함께 다루고, 관리자 대시보드와 웹 거래 화면(HTS)을 제공합니다.

## 버전 계보

| 버전 | 코드명 | 상태 |
|---|---|---|
| 1.0 | Simpsons | 이전 버전 |
| 2.0 | ArcadiaBene | 이전 버전 |
| **3.0 Dawn** | **Laplace** | **현재 버전** |
| 3.0 Dusk | Fourier | 예정 |
| 3.0 Midnight | El!xer | 예정 |

## 구성

| 구성 요소 | 위치 | 주소 |
|---|---|---|
| 관리자 대시보드 | `apps/dashboard` | `http://127.0.0.1:4310` |
| HTS (거래 화면) | `apps/hts` | `http://127.0.0.1:4311` |
| API (TypeScript) | `apps/api` | `http://127.0.0.1:4312/api/health` |
| Python 데이터 서비스 (비공개) | `web_api` | `127.0.0.1:4313` (API가 자동으로 시작) |
| 공식 사이트 (NACURI) | `apps/site` | `http://127.0.0.1:4314` |
| Wide-Learning 뷰어 | `apps/wl` | `http://127.0.0.1:4315` |

- 디스코드 봇과 도메인 로직은 Python으로 작성되어 있습니다. 진입점은 `main.py`와 `run.py`이고, 로직은 `managers/`, `database/`, `functions/`에 있습니다.
- 운영 DB는 `database/inazina.db` 하나입니다. 큰 금액(10^18 이상)을 오차 없이 저장하는 형식을 쓰므로, 이 형식을 읽는 현재 코드로만 열어야 합니다.
- 모듈 계층과 의존 규칙은 [docs/architecture.md](docs/architecture.md)에 있습니다.

## 시작하기

필요한 것: Node.js 24 이상, Python 3 (프로젝트 루트의 `.venv` 권장).

```
npm install
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt   # 테스트 도구(pytest) — 개발할 때만
```

디스코드 봇을 쓰려면 프로젝트 루트 `.env`에 `INAZINA_TOKEN=봇_토큰`을 넣습니다.

전체 서비스를 한 번에 실행합니다 (종료는 `Ctrl+C`).

```
./start_all.command
```

개별 실행:

```
npm run dev:api        # API (Python 데이터 서비스도 함께 시작)
npm run dev:dashboard  # 관리자 대시보드
npm run dev:hts        # HTS
npm run dev:site       # 공식 사이트
npm run dev:wl         # Wide-Learning 뷰어
```

## 검사

```
.venv/bin/python -m pytest -n auto   # Python 테스트
npm run typecheck                    # TypeScript 타입 검사
npm run build                        # 전체 앱 빌드
```

## 문서

처음이라면 [인수인계서](docs/HANDOVER.md)부터 읽으세요. 서비스 구성, 코드 흐름 도식, 운영 방법, 알려진 부채가 있습니다.

- [docs/HANDOVER.md](docs/HANDOVER.md) — 인수인계서와 코드 흐름 도식
- [docs/architecture.md](docs/architecture.md) — 모듈 구조와 경계
- [docs/operations.md](docs/operations.md) — 운영 안내와 관리자 명령어
- [docs/products.md](docs/products.md) — 옵션·우선주·ELS·복권·시장 정지
- [docs/finance.md](docs/finance.md) — 통화·재무제표·신용점수
- [docs/corporate.md](docs/corporate.md) — 기업·주식 구조
- [docs/wide_money.md](docs/wide_money.md) — 10^18 이상 금액 처리
- [docs/roadmap_laplace.md](docs/roadmap_laplace.md) — LAPLACE 로드맵
- [docs/performance.md](docs/performance.md) — 성능 기록
- [docs/ui.md](docs/ui.md) — 화면 설계
- [CHANGELOG.md](CHANGELOG.md) — 변경 내역

## 공동 개발자

- [Winter Lanuarii Lee (@winterliy)](https://github.com/winterliy)
- OpenAI Codex (AI 공동 개발자)

[GitHub 전체 기여자 보기](https://github.com/winterlies/INAZINA/graphs/contributors)
