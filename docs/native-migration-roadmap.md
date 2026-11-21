# 네이티브 이식 로드맵 (초안)

> 상태: **초안**. 2026-11-21 논의 기준이며, 나중에 바꾸기 위해 남겨 둔 문서다.
> 아래 "미확정 사항"을 먼저 확인한 뒤 수정한다. 파일 분류는 파일명과 `docs/architecture.md`를 근거로 했고, 파일 내용을 하나씩 읽고 정한 것은 아니다.

## 1. 목적과 범위

- 목적: **속도**, **장기적인 파이썬 탈피**.
- 범위: 디스코드 계층만 파이썬으로 남기고 백엔드 전부를 이식한다 (범위 C).
- 조건:
  - 파일 간 호출은 **프로세스 내 FFI**로 한다. 파일을 중간에 두고 주고받지 않는다.
  - **SQL은 SQL 그대로** 둔다. 쿼리는 `.sql`로 분리해 언어와 무관하게 공유한다.
  - **HTTP API 계약(경로, JSON 형식)은 바꾸지 않는다.**
  - NumPy, JIT, CPython 최적화는 **파이썬으로 남기는 계층에만** 쓴다. 백엔드에는 쓰지 않는다.

## 2. 구성 요소 분류

| 분류 | 위치 | 비고 |
|---|---|---|
| 내부연산 백엔드 | `managers/`(70개), `functions/`의 계산·행위 모듈 | L0~L3 계층 (`docs/architecture.md`) |
| 디스코드 프론트엔드 | `main.py`, `run.py`, `functions/`의 View·명령·임베드 | 파이썬 유지 |
| 데이터베이스 백엔드 | `database/`, `tools/`의 DB 도구 | |
| 웹 프론트엔드 | `apps/dashboard`, `hts`, `site`, `wl`, `shared` | TS 유지 |
| 기타 프론트/백 | `apps/api`, `web_api/`, `tools/`, 설정, 테스트, 문서 | |

제외: `.claude/worktrees/*`, `node_modules`, `.venv`, `dist`, 각종 캐시.

## 3. 언어 배정

선택 기준: Rust와 C++는 C ABI로 파이썬과 서로를 호출할 수 있고 GC가 없다. Go는 프로세스 내 공유에 불리해서 독립 실행 파일에만 쓴다. Java와 Kotlin은 JVM 내장 비용과 약한 네이티브 생태계 때문에 쓰지 않는다. 어셈블리는 별도 파일로 두지 않고, 측정으로 필요가 확인된 커널만 C++ 인트린식으로 쓴다.

| 영역 | 대상 | 언어 |
|---|---|---|
| 가격·수치 엔진 | `_price_math`, `price_backend`, `stochastic_price_engine`, `auto_target_engine`, `price_gap_filler` | **C++** |
| 파생상품 평가 | `option_manager`, `futures_manager`, `els_manager`의 가격·그릭스·시뮬 부분 | **C++** |
| 계산 유틸 | `technical_indicators`, `interest_calc`, `tax_calc`, `rate_schedule`, `recovery_waterfall`, `dr_pricing` | **C++**(반복 수치) / **Rust**(분기 많은 규칙) |
| 큰 금액·정밀 연산 | `money`, `exact_money`, `fractional`, `wide_values`, `wide_sql`, `exact_cost_basis` | **Rust** (필요 시 128비트 커널만 C++ 인트린식) |
| 원장·잔액·트랜잭션 | `currency_manager`, `financial_manager`, `bank_journal`, `accounting_journal`, `deposit_accounts`, `pnl_ledger` | **Rust** |
| 주문 체결 | `order_book_manager`, `order_validation`, `market_session_manager`, `trading_warning_manager` | **Rust** (매칭 루프는 측정 후 C++ 여부 결정) |
| 도메인 로직 | 은행, 이자, 환전, 채권, 펀드, 투자, 기업행위, 복권, 준법, 신용위험, 파산·회생, 귀화 등 L1~L2 | **Rust** |
| 관리자 계층 | `admin_manager`, `admin_control_manager`, `catalog_manager` | **Rust** |
| DB 접근 계층 | `database/*_db.py`, `pool`, `pg_backend`, `integrity`, `unified` | **Rust** (`sqlx` 또는 `tokio-postgres`) |
| 스키마·트리거 | `database/postgres/`, `pg_schema_sync`, `build_postgres_*` | **SQL** (실행 스크립트는 Rust) |
| 웹 데이터 서비스 | `web_api/*` | **Rust** (`axum`, 라우트와 JSON 형식 유지) |
| API 게이트웨이 | `apps/api` | **Rust** (`web_api`와 한 서버로 통합, 결정 사항은 8절 참고. 잠정) |
| 운영 도구 | `geoip_lookup`, `update_geoip`, `verify_backup`, `backup_restore_drill`, `reset_access_password`, `repair_ledger`, `pg_cutover_check`, `wlmd_check` | **Go** (독립 실행 파일) |
| 일회성 이관 도구 | `sqlite_to_postgres`, `rename_country`, `migrate_*` | **Python 유지** |
| 알림 저장 | `notification_manager` (DB 테이블 읽기·쓰기만 함, 디스코드 import 없음) | **Rust** (디스코드 발송은 파이썬 계층이 `pending_discord`를 읽어 처리) |
| 차트 렌더링 | `price_chart`, `indicator_chart` (순수 파이썬으로 PNG를 직접 생성, 디스코드·NumPy 의존 없음) | **Rust** (후순위, 또는 파이썬 유지) |
| 디스코드 계층 | `main.py`, `functions/*_views`, `slash_commands`, `web_jobs`(웹 작업을 디스코드로 연결) | **Python 유지** |
| 웹 프론트 | `apps/*` | **TS 유지** |

### NumPy, JIT, CPython 최적화의 적용 범위

| 대상 | 적용 여부 |
|---|---|
| 백엔드 (C++, Rust, Go) | 쓰지 않는다. NumPy, Numba 중간 단계는 삭제했다 |
| 디스코드 계층(파이썬) | CPython 내장 JIT 측정, Cython/mypyc, 필요하면 NumPy (현재 차트 코드는 NumPy를 쓰지 않는다) |
| 웹 프론트(TS) | 해당 없음 (브라우저 엔진이 자체 JIT을 가짐) |

예외: FFI 경계에서는 CPython 규칙을 지킨다. 최적화가 아니라 바인딩의 필수 조건이다.
- 네이티브 계산 구간에서는 **GIL을 푼다** (Rust `py.allow_threads`, C++ `py::gil_scoped_release`). 디스코드 봇이 asyncio 한 스레드로 돌기 때문에, 풀지 않으면 봇 전체가 멈춘다.
- ABI를 정한다: 버전별 모듈(현재 방식, 가장 빠름) 또는 `abi3`(재빌드 부담이 적음).

## 4. 소통 방식

- 파이썬과 네이티브: **프로세스 내 FFI** (Rust는 PyO3 + maturin, C++는 pybind11). 함수 시그니처는 유지하고 안쪽 구현만 교체한다.
- Rust와 C++ 사이: `cxx` 또는 C ABI. 한 모듈에서 C++ 호출은 Rust 래퍼로 감싸, 파이썬에는 Rust 쪽 하나만 보이게 한다.
- 배열은 복사 없이 넘긴다 (`rust-numpy`, pybind11 버퍼 프로토콜). 호출은 가능하면 배열 단위로 설계한다.
- DB: 같은 PostgreSQL. 이식 중에는 파이썬과 네이티브가 같은 테이블을 동시에 쓸 수 있으므로, 도메인별로 "이 테이블은 이제 Rust 소유"를 아래 5절 표로 관리해 이중 쓰기를 막는다.
- 금액은 float로 다루지 않는다. 수치 계산 결과를 금액에 반영할 때는 경계에서 정수로 변환한다.

## 5. 테이블 소유권 표 (비어 있음, 이식 시작 시 채운다)

| 도메인 | 테이블 | 현재 소유 | 이식 후 소유 | 전환일 |
|---|---|---|---|---|
| (예) 통화·잔액 | | Python | Rust | |

## 6. 이식 순서

1. **PostgreSQL 전환을 먼저 끝낸다.** 네이티브 코드가 붙을 DB를 하나로 고정한다 (`docs/postgres-cutover-runbook.md` 참고).
2. **프로파일링**으로 실제로 느린 곳을 확정한다. 이 문서의 C++ 대상은 추정이므로 측정 결과로 바꾼다.
3. **C++**: 가격·수치 커널 (GIL 해제 포함). 현재 `_price_math_native`는 C++이 아니라 **순수 파이썬 `_price_math.py`를 Cython으로 컴파일한 것**이다(8절 참고). 그래서 C++ 이식은 이어 쓰는 것이 아니라 새로 짜는 것이며, 기존 `tools/benchmark_price_native.py`의 python/cython 비교 방식을 C++ 후보를 비교하는 기준선으로 쓴다.
4. **Rust**: 큰 금액 → 원장 → 주문 → 도메인(L0 → L1 → L2 → L3 순) → DB 접근 계층 → 웹 서비스.
5. **Go**: 운영 도구.
6. **파이썬 계층**: 내장 JIT 측정, Cython/mypyc (NumPy는 필요할 때만).

각 매니저를 옮길 때마다:
- 기존 파이썬 구현과 새 구현을 **같은 테스트(`tests/`)로 돌려 결과를 비교**한다.
- `tests/test_architecture.py`의 계층 규칙(같은 계층이나 아래 계층만 import)을 네이티브 모듈에도 적용한다.
- L2 내부의 순환 의존(`bank ↔ interest ↔ currency_exchange` 등)은 이식 전에 공통 부분을 L1로 내려서 끊는다.

## 7. 위험과 주의

- **두 언어 공존의 빌드 복잡도**: Rust와 C++이 섞이면 빌드와 파이썬 노출이 두 갈래가 된다. 위 4절의 래퍼 규칙으로 줄인다.
- **PG 전환과의 충돌**: DB 계층을 PG 전환 중에 다시 쓰면 같은 곳을 두 번 건드린다. 6절 1번을 선행한다.
- **트랜잭션 경계**: 통화·국고·주문·계좌가 한 트랜잭션에서 바뀌어야 한다는 모놀리스 전제(`docs/architecture.md`)를 깨지 않는다. 한 트랜잭션에 걸치는 매니저들은 같은 시점에 함께 옮긴다.
- **C++ 범위는 추정**: 프로파일링 없이 확정하면 낭비가 생긴다.

## 8. 미확정 사항 (수정 시 먼저 확인)

### 확인 완료 (2026-11-21)

1. **파이썬 버전**: `.venv`는 CPython **3.14.7**, **GIL 켜짐**(free-threaded 아님, `Py_GIL_DISABLED=0`). 따라서 FFI 경계에서 GIL 해제가 필요하다는 전제는 그대로 유효하다. ABI(abi3 / 버전별)는 아직 선택하지 않았다.
2. **`_price_math_native`의 정체**: C++이 아니다. `build_price_native.py`가 **순수 파이썬 `managers/_price_math.py`를 Cython으로 컴파일**한 선택적 모듈이며(`Cython==3.3.0`), `-ffp-contract=off`로 부동소수 결과를 파이썬과 맞춘다. 원본 해시를 넣어 오래된 빌드를 `tools/benchmark_price_native.py`가 거부한다. 이 `.so`는 arm64와 x86_64 유니버설 바이너리다.
   - 결과: 가격 엔진은 이미 "파이썬 원본 + Cython 가속" 구조이므로, C++ 이식의 정답지는 `_price_math.py`와 이 벤치마크다.
   - 주의: 문서의 "Cython은 파이썬 계층에만 쓴다"는 원칙과 달리, 현재 코드는 백엔드(가격 엔진)에 Cython을 쓰고 있다. C++ 이식이 끝나면 이 빌드는 제거 대상이다.
3. **분류가 모호했던 파일**:
   - `price_chart`, `indicator_chart`: 디스코드 import가 없고 NumPy도 쓰지 않는다. `struct`와 `zlib`로 PNG를 직접 만드는 **순수 파이썬 렌더러**다(`indicator_chart`가 `price_chart._Canvas`를 쓴다). 디스코드 계층이 아니라 백엔드 연산으로 분류하고, 이식 우선순위는 낮다.
   - `web_jobs`: `asyncio`로 웹 작업을 받아 `governance_notices.convene_from_discord(bot, ...)`를 호출한다. **디스코드 연결 코드이므로 파이썬 유지**.
   - `notification_manager`: 디스코드 import 없이 `notifications` 테이블만 읽고 쓰며, `discord_sent_epoch` 열로 발송 여부만 표시한다(`pending_discord`, `mark_discord_sent`). **백엔드(Rust)로 분류**하고, 실제 발송은 파이썬 디스코드 계층이 맡는다.
4. **루트의 `postgresql:/` 폴더**: 안에 비어 있는 `backups/` 폴더만 있다(생성 2026-11-19 00:11, `.env`와 `main.py` 수정 시각과 거의 같다). 내용이 없어 지워도 데이터 손실은 없어 보이지만, 왜 생겼는지는 확인하지 못했다. `postgresql://...` 형태의 접속 문자열이 폴더 경로로 쓰였을 가능성이 있으나 **추정**이다. 삭제 여부는 사용자가 정한다.

### 아직 미확정

3. **웹 서비스 방향 (잠정 결정, 2026-11-21)**: `apps/api`(TS)와 `web_api`(Python)를 **둘 다 Rust(`axum`) 한 서버로 통합**한다. 코어와 같은 프로세스에서 직접 호출하므로 중간 HTTP 왕복과 JSON 변환이 없어져 속도 이득이 가장 크다. 브라우저에 보이는 HTTP 경로와 JSON 형식은 그대로 유지한다.
   - 전제: 코어(원장, 도메인, DB 접근)가 먼저 Rust로 옮겨져 있어야 한다. 그렇지 않으면 Rust 서버가 Python을 다시 불러야 해서 이득이 없다.
   - **`apps/api` 확인 결과**: 단순 중계가 아니다. `src/index.ts` 1,401줄, `landing.ts` 110줄, `browserAccess.ts` 21줄이고 `/api/*` 경로가 80개 안팎이다. 통합하려면 아래를 모두 Rust로 옮겨야 한다.
     - **인증·세션**: HMAC 서명 쿠키(세션, 디스코드 인증용), `authVersion` 검사, 관리자 대리 로그인(`login-as`, `by`), 로그인 실패 횟수 제한.
     - **디스코드 OAuth**: 시작·콜백·검증, state 쿠키와 논스 재사용 방지.
     - **브라우저 접근 게이트**: 4312 접근 로그인 페이지, 비상 암호 경로, 시도 로그(`database/browser-access.log.jsonl`, 파이썬 `security_store.py`와 공유), 랜딩 페이지(`apps/shared/gate.html`, `scene.svg` 사용).
     - **기타 직접 처리**: 사이트 피드백, 게시판 조회수 중복 방지, 게시판 파일 업로드·다운로드, 검색 지표 차트, 관리자 전용 권한 검사(`role !== "admin"`).
     - **파이썬 프로세스 관리**: `web_api/server.py`를 자식 프로세스로 띄우고, 죽으면 재시작하며(지수 백오프), `INAZINA_PYTHON_WORKERS`로 최대 8개까지 포트를 나눠 띄운다. 파이썬 쪽이 CPU 병목(프로세스당 GIL 하나)이라 만든 우회로이며, Rust 통합 서버가 되면 이 부분은 필요 없어진다.
     - **프로세스 안의 메모리 상태**: 로그인 시도 횟수, OAuth state, 사용한 논스, 게시판 조회 기록. 한 서버로 합치면 이 상태가 하나의 프로세스에 모이므로 오히려 단순해지지만, 재시작 때 사라지는 점은 지금과 같다.
     - **파이썬과의 내부 규약**: 게이트웨이는 `/internal/*` 경로를 `x-inazina-internal-token`, `x-inazina-user-id`, `x-inazina-delegated-by` 헤더와 함께 호출한다. 이 경로는 외부에 보이지 않는 사설 인터페이스라서 통합하면 없애도 되고, 외부에 보이는 `/api/*` 계약만 유지하면 된다.
   - **이식 순서 (확정)**: 일반 라우트를 먼저 옮기고, **인증·세션, 디스코드 OAuth, 브라우저 접근 게이트는 마지막에** 옮긴다. 보안 코드는 실수가 곧 보안 문제이므로, 옮기기 전에 기존 동작을 고정하는 테스트(로그인, 실패 횟수 제한, 쿠키 서명·만료, OAuth state·논스, 비상 암호 경로)를 먼저 만든다.
   - 결론: 통합은 "라우트 옮기기"가 아니라 **인증·보안 기능을 포함한 게이트웨이 전체 재작성**이다. `web_api`만 Rust로 바꾸는 대안이 변경 폭이 훨씬 작다는 점은 그대로다.
   - 대안(보류): `web_api`만 먼저 Rust로 바꾸고 TS 게이트웨이는 나중에 합친다. 변경 폭이 작고 되돌리기 쉽지만 HTTP 왕복이 한 번 남는다.
   - 이식 순서상 코어 이식이 끝난 뒤에 진행한다 (6절).
아래는 **나중에 정하기로 함** (2026-11-21).

6. 파이썬 계층에서 내장 JIT과 free-threaded(3.14t)를 쓸지 (파이썬에 남는 계층에만 영향, 도입 시점에 측정).
7. 비동기 DB 접근(`tokio`)을 쓸 때 파이썬 asyncio와의 경계를 어떻게 잡을지 (이식 시작 시 설계).
8. `postgresql:/` 폴더를 지울지.
9. ABI 선택: 버전별 모듈(빠름, 현재 방식) 또는 `abi3`(재빌드 부담이 적음).

## 9. 변경 기록

| 날짜 | 내용 |
|---|---|
| 2026-11-21 | 초안 작성 |
| 2026-11-21 | 미확정 4개(파이썬 버전, `_price_math` 정체, 모호한 파일 분류, `postgresql:/`) 확인 후 반영. `_price_math_native`가 C++이 아니라 Cython임을 정정 |
| 2026-11-21 | 웹 서비스 방향을 "`apps/api`와 `web_api`를 Rust 한 서버로 통합"으로 잠정 기록 (8절 3번) |
| 2026-11-21 | `apps/api` 코드를 읽고 범위를 기록: 중계가 아닌 인증·OAuth·접근 게이트·업로드·프로세스 관리를 포함한 게이트웨이 전체 |
| 2026-11-21 | 게이트웨이 이식 순서 확정(보안 코드는 마지막, 테스트 먼저). 6·7·8·9번은 나중에 정하기로 함 |
