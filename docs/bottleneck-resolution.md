# 병목 해결 계획 (초안)

> 상태: **A·B 실행 완료(2026-11-21), C는 하지 않기로 함.** 결과는 11절. 2026-11-21 프로파일링 결과를 바탕으로 쓴 계획이다.
> **속도 병목을 어떻게 줄일지**만 다룬다. 기존 성능 기록은 `docs/performance.md`에 있다.
> 2026-11-21에 Rust/C++ 등으로의 언어 변경(네이티브 이식) 계획은 **폐기**했고, 이 문서의 계획만 유지한다. 폐기된 계획은 git 기록(`e39355e`의 `docs/native-migration-roadmap.md`)에 남아 있다.

## 1. 요약

- 시장 가격 틱 1회는 **809ms**(부하 낮은 쪽 측정)~1.5초(부하 높을 때)이고, 그중 DB 시간이 약 700ms, 파이썬 계산이 약 110ms다. 틱 시간의 **78~86%가 PostgreSQL 응답 대기**다.
- 가격 계산(`stochastic_price_engine`, `auto_target_engine`)은 **1% 미만**이다. 계산을 C++/Rust로 옮겨도 틱은 거의 빨라지지 않는다.
- 원인은 DB 호출 방식이다. 이 문서의 계획은 언어를 바꾸지 않고 파이썬 안에서 해결한다.
  1. `pg_backend.py`의 `execute`는 트랜잭션 안에서 문장마다 `SAVEPOINT` → 문장 → `RELEASE SAVEPOINT`를 보낸다. 논리 문장 893개가 실제 왕복 **2,680번**이고, 그중 66%가 savepoint 처리다.
  2. `PgCursor.executemany`는 행마다 위 `execute`를 부르는 파이썬 루프다. 한 행이 왕복 3번이다.
  3. 틱 하나에서 같은 모양의 쿼리가 수백 번 반복된다. 다섯 가지 쿼리가 틱의 약 80%(649ms/809ms)를 쓴다.
- 예상 효과: 틱 809ms → 약 **250~350ms** (추정, 검증 전).

## 2. 측정 방법과 환경

| 항목 | 내용 |
|---|---|
| 데이터 | `database/backups/inazina-before-pg-20261119-000909.db` (PG 전환 직전의 SQLite 백업)를 임시 폴더로 복사 |
| 적재 | 스크래치 DB `inazina_profile_20261121`(로컬 PostgreSQL 17)에 `tools/sqlite_to_postgres.py`로 적재 (256개 테이블, 644,686행, 112MB). 적재 후 `ANALYZE` |
| 운영 보호 | 운영 DB(`postgresql:///inazina`)는 쓰지 않았다. 측정 스크립트는 DSN이 스크래치 DB가 아니면 실행을 거부하도록 했다 |
| 대상 | `MarketManager.update_prices` 전체 (시뮬레이션 상태 169개, 가격 이력 54.7만 행) |
| 가격 백엔드 | `python` (`price_backend.py`의 기본값) |
| 라이브러리 | Python 3.14.7(GIL 켜짐), psycopg 3.3.9(binary) |

### 재현 순서
1. 백업을 임시 폴더로 복사한다 (원본 백업 파일에 직접 연결하지 않는다).
2. `createdb inazina_profile_<날짜>`로 빈 스크래치 DB를 만든다.
3. `INAZINA_PG_DSN=` 를 비운 채 `.venv/bin/python tools/sqlite_to_postgres.py <복사본>.db "postgresql:///inazina_profile_<날짜>"`로 적재한다. 이 도구는 **대상 DB의 `public` 스키마를 지우므로** 대상이 스크래치 DB인지 반드시 확인한다. 약 몇 분 걸린다.
4. `psql -d inazina_profile_<날짜> -c ANALYZE`로 통계를 갱신한다.
5. 아래 부록의 스크립트를 `INAZINA_DB_BACKEND=postgres INAZINA_PG_DSN=postgresql:///inazina_profile_<날짜>` 환경에서 실행한다. (스크립트 안의 DSN 검사 문자열도 같은 DB 이름으로 맞춘다.)

### 현재 남아 있는 것 (2026-11-21 시점, 지워졌을 수 있으니 확인)
- 스크래치 DB `inazina_profile_20261121` (112MB). 지울 때: `dropdb inazina_profile_20261121`.
- 작업 임시 폴더의 백업 복사본(265MB)과 스크립트(`prof_tick.py`, `prof_kernels.py`, `prof_sql.py`). 스크립트는 이 문서 부록에도 있다.

## 3. 측정 결과

### 3.1 1차: `cProfile` (부하 높음, load average 15~19)

| 항목 | 수치 |
|---|---|
| 틱 1회, 프로파일러 끔 | 중앙값 약 1.5~1.8초, p95 약 2.4~3.2초 |
| 첫 틱(백업 이후 밀린 구간 따라잡기 포함) | 1.8~3.7초 |
| `psycopg` 응답 대기 | 자기 시간의 78~86% |
| `executemany` | 누적 시간의 약 69% |
| 가격 계산이 차지하는 비율 | 1% 미만 |
| 가격 계산 커널 1회(`simulate_step`+`target_pull`+`cycle_bounds`) | 파이썬 약 31.6µs |

### 3.2 2차: SQL 모양별 계측 (부하 낮음, load average 약 13, 5틱 평균)

| 항목 | 수치 |
|---|---|
| 틱 1회 | **809ms** |
| 틱당 논리 문장 수 | 893 |
| 틱당 실제 왕복 수 | **2,680** (문장 901 + `SAVEPOINT`/`RELEASE` 1,779) |
| 문장 왕복 시간 / savepoint 왕복 시간 | 481ms / 218ms |

틱당 시간이 큰 쿼리 (모두 행마다 반복):

| 쿼리 | 횟수 | 시간 | 원인 위치 |
|---|---|---|---|
| `UPDATE company.company SET share_price = ? WHERE company_code = ?` | 164 | 193ms | `managers/market_manager.py` `price_updates`를 `executemany`(약 967~972행 부근) |
| `INSERT INTO assets.market_price_history (asset_code, price, recorded_epoch)` | 167 | 185ms | 같은 파일의 가격 이력 기록 |
| `INSERT INTO market_simulation_state ... ON CONFLICT DO UPDATE` | 167 | 124ms | `_flush_tick`의 `executemany`(약 795행) |
| `INSERT INTO assets.market_exchange_index_components` | 154 | 74ms | `_update_exchange_indices` |
| `SELECT * FROM market_exchanges WHERE exchange_id=?` | 174 | 73ms | `managers/market_session_manager.py` `_row`(약 78행)가 회사마다 같은 거래소를 반복 조회 |

합계 649ms로 틱의 약 80%다. 나머지 DB 문장은 모두 10ms 이하다. 파이썬 계산은 약 110ms다.

### 3.3 이전 기록과의 비교
`docs/performance.md`에는 2026-11-08에 운영 DB의 읽기 전용 복사본(SQLite)으로 쟀을 때 전체 갱신 중앙값이 Python 198ms, Cython 193ms였다는 기록이 있다. 이번에는 PostgreSQL에서 809ms~1.5초다.
- 단순 비교는 위험하다. 데이터 크기, 시뮬레이션 상태 수, 기계 부하가 다르다.
- 그래도 **SQLite(프로세스 안)에서 PostgreSQL(소켓 왕복)로 바뀌며 문장 하나의 비용이 커졌고, savepoint 3중 왕복이 그 비용을 3배로 키웠다**는 설명이 측정과 맞는다. 확정은 아니다.

### 3.4 측정의 한계
- 시장 틱 **한 가지 시나리오**만 쟀다. 주문 체결, 웹 API 요청, 은행·이자 처리는 아직 재지 않았다(4.5 참고).
- 기계 부하가 높고 변해서 **절대 시간은 믿기 어렵다.** 일관된 것은 DB 대기 비율과 호출 수다.
- 스크래치 DB는 방금 적재한 상태라 운영의 캐시 상태와 데이터 분포가 다르다.
- 백업이 11월 19일 시점이라 최근 데이터가 반영되어 있지 않다.

## 4. 해결 계획 (효과 큰 순서)

### 4.0 시작 전 확인
- `git status`로 수정할 파일(`database/pg_backend.py`, `managers/market_session_manager.py`, `managers/market_manager.py`)에 다른 세션의 미커밋 변경이 없는지 확인한다. (2026-11-21 시점에는 이 파일들에 변경이 없었다.)
- 같은 스크래치 DB를 **2개로 복제**해 둔다: `createdb -T inazina_profile_20261121 inazina_profile_base`, `createdb -T inazina_profile_20261121 inazina_profile_new`. 다른 연결이 열려 있으면 `-T` 복제가 실패하므로 스크립트를 먼저 끝낸다.
- 개선 전 기준선(틱 시간, 틱당 왕복 수, 테이블 내용)을 `base`에서 먼저 기록한다.

### 4.1 A. `PgCursor.executemany` 교체 (`database/pg_backend.py`)
현재는 행마다 `self.execute`를 불러 행당 왕복 3번이다.
- 배치 전체를 `SAVEPOINT` 하나로 감싸고, 내부는 psycopg의 `cursor.executemany`로 보낸다. psycopg 3.x의 `executemany`는 파이프라인 방식이라 행마다 응답을 기다리지 않는다. 설치된 3.3.9에서 실제로 그렇게 동작하는지 구현 때 확인한다.
- `translate(sql)`은 배치당 한 번만 부르고, 파라미터는 기존 `_params`로 변환한다.
- `RETURNING`/`lastrowid`가 필요한 문장은 이 경로에서 제외한다(기존 `execute` 경로 유지). `executemany`에서 `lastrowid`를 쓰는 호출자가 있는지 확인한다.
- `rowcount`는 기존처럼 합계로 맞춘다.
- **의미 차이**: 배치 중간에 실패하면 지금은 앞 행이 남고, 새 방식은 배치 전체가 savepoint로 롤백된다. 에러를 잡고 계속 진행하는 `executemany` 호출자가 있는지 `git grep -n "executemany"`로 전수 확인하고, 있다면 그 호출자만 기존 경로를 쓰게 한다.
- 예상 효과: 약 **500ms** 감소 (위 표의 앞 네 항목). 앱 전체의 `executemany`(은행, 이자 등)도 빨라진다.

### 4.2 B. 거래소 행 반복 조회 제거 (`managers/market_session_manager.py`)
`_row(conn, exchange_id)`가 같은 거래소를 회사마다 `SELECT *`로 다시 읽는다(틱당 174번).
- 틱(또는 같은 트랜잭션) 안에서 거래소 행을 한 번에 읽어 두고 재사용한다. 같은 모듈의 `INSERT`/`UPDATE`가 있으면 캐시를 갱신하거나 비운다.
- `_row`는 `classmethod`이고 `conn`을 받는다. 캐시 키는 `conn` 객체와 `exchange_id`로 하고, 트랜잭션이 끝나면 버린다.
- 캐시가 다른 경로에서 읽는 오래된 값을 내보내지 않는지 확인한다: 관리자 조작이나 장 전환이 틱 중간에 일어나는 경로.
- 예상 효과: 약 **70ms** 감소.

### 4.3 C. 읽기 문장의 savepoint 줄이기 (A, B 후 재측정해서 결정)
읽기 전용 `SELECT`는 savepoint 없이 보낼 수 있다. 다만 PostgreSQL은 실패한 문장이 트랜잭션 전체를 중단시키므로, `SELECT` 에러를 잡고 계속 진행하는 호출자가 있으면 위험하다. A와 B 후에도 savepoint 왕복이 크면 그때 판단하고, 아니면 하지 않는다.

### 4.4 D. 재측정과 남은 시간 분석
- A와 B 후 같은 스크립트로 다시 잰다. 남은 시간이 DB인지 파이썬 계산인지 확인한다.
- 남은 파이썬 계산(약 110ms), `movement_history.cleanup`, `ELSManager.observe`를 `cProfile`로 본다.
- 남은 병목이 파이썬 계산이면 그 함수만 따로 잰 뒤 **파이썬 안에서** 알고리즘, 자료구조, 불필요한 변환 제거로 개선한다. 언어 변경은 계획에 포함하지 않는다.

### 4.5 E. 다른 시나리오 측정 (코드 변경 없음, 병행 가능)
같은 방식(스크래치 DB, 호출 수와 SQL 모양별 시간)으로 아래를 잰다. 같은 패턴(문장당 savepoint 왕복, 반복 쿼리)이 나오면 A의 효과가 더 넓다는 뜻이다.
- 주문 접수·체결
- 웹 API 요청 몇 개 (포트폴리오, 주문 조회, 관리자 개요)
- 이자·은행 처리
- 부하가 낮은 시간에 시장 틱을 한 번 더 재서 3.3의 설명을 확인한다.

## 5. 검증 방법
- **전후 비교**: `base`는 기존 코드, `new`는 수정 코드로 돌리되 **같은 시드**(`random.seed(42 + tick)`)와 같은 `now`를 쓴다. 번갈아 여러 번 재고 **비율**로 판정한다.
- **결과 동일성**: 같은 틱 수를 돌린 뒤 `base`와 `new`의 `market_assets`, `company`, `investment`, `market_simulation_state`, `market_auto_targets`, `market_price_history`, `market_exchanges`, `market_exchange_index_history`를 `ORDER BY 1`로 전부 비교한다. 부동소수 값이 하나라도 다르면 실패다. (`tools/benchmark_price_native.py`의 동등성 검사와 같은 방식)
  - 주의: 가격 시뮬레이션은 부동소수 결과가 달라지면 동등성 검사에 실패한 전례가 있다(`docs/performance.md`, 2026-11-08). **쿼리만 묶고 계산 순서와 값은 바꾸지 않는다.**
- **테스트**: PG 백엔드, 시장, 아키텍처 테스트를 돌린다. 먼저 `ls tests | grep -i -E "pg|market|backend|architecture"`로 대상 파일을 고른다. A는 앱 전체의 `executemany`에 영향이 가므로 가능하면 전체 테스트를 돌린다. 테스트는 같은 PostgreSQL에 `inazina_test_*` DB를 만들므로, 다른 세션이 돌리는 중이면 부하가 섞인다.

## 6. 성공 기준 (초안, 나중에 조정)
- 같은 조건에서 시장 틱 중앙값이 기준선 대비 **의미 있게 줄어든다.** (예상 250~350ms. 목표 수치는 기준선을 다시 잰 뒤 정한다.)
- 틱당 실제 왕복 수가 2,680에서 크게 줄어든다.
- `base`와 `new`의 테이블 내용이 **정확히 일치**한다.
- 기존 테스트가 모두 통과한다.

## 7. 위험과 주의
- **A의 범위**: 앱 전체의 `executemany`에 영향이 간다. 호출자 전수 확인과 전체 테스트가 필수다.
- **캐시의 정합성(B)**: 장 상태를 틱 안에서 캐시하면, 틱 중간에 상태가 바뀌는 경우(관리자 조작, 장 전환)를 놓칠 수 있다.
- **측정 오염**: 다른 세션의 테스트가 같은 PostgreSQL을 쓰고 있어 부하가 변한다. 비교는 항상 번갈아 여러 번 잰다.
- **운영 DB 보호**: 측정과 수정 확인은 스크래치 DB에서만 한다. 스크립트의 DSN 검사를 지우지 않는다.
- **스크래치 DB 관리**: 끝나면 `dropdb`로 `inazina_profile_*`를 지우고, 임시 폴더의 백업 복사본도 정리한다.

## 8. 미확정 사항
1. 4.5(다른 시나리오 측정)를 어디까지 할지.
2. 4.3(읽기 문장 savepoint)을 할지 (A, B 후 재측정으로 결정).
3. 측정 스크립트를 `tools/`로 옮겨 저장소에 둘지 (지금은 이 문서 부록에만 있다).
4. psycopg 3.3.9의 `executemany` 파이프라인 동작을 구현 때 확인한 결과.

## 9. 실행 세션 인수 메모
- 이 문서는 초안이며 **아직 어떤 코드도 수정하지 않았다.** 운영 코드, 운영 DB, `.so` 모듈은 그대로다.
- 이 문서는 커밋되어 있지 않을 수 있다. `git status docs/bottleneck-resolution.md`로 확인한다.
- 네이티브 Cython 모듈 `managers/_price_math_native*.so`는 원본과 해시가 달라 stale 상태이고, 기본 백엔드가 `python`이라 운영에는 영향이 없다. 이 계획과 무관하니 재빌드하지 않는다.
- 4.1(A)부터 시작하고, 각 단계마다 5절의 전후 비교와 동일성 검사를 통과한 뒤 다음으로 넘어간다.

## 10. 변경 기록

| 날짜 | 내용 |
|---|---|
| 2026-11-21 | 초안 작성. 시장 틱 프로파일링 결과와 해결 계획 기록 |
| 2026-11-21 | 언어 변경(네이티브 이식) 계획 폐기에 맞춰 이식 관련 서술을 제거 |
| 2026-11-21 | SQL 모양별 계측(2차) 결과와 구체적 해결 계획(A~E), 검증 방법, 실행 세션 인수 메모, 측정 스크립트 부록 추가 |

## 부록. 측정 스크립트

임시 폴더에 있던 것을 그대로 옮겼다. 스크래치 DB 이름을 바꿨다면 `assert` 문의 DSN도 같이 바꾼다. 운영 DB를 건드리지 않도록 이 검사는 지우지 않는다.

### A. `prof_tick.py` — 틱 전체를 `cProfile`로 측정

```python
"""Profile MarketManager.update_prices on the SCRATCH PostgreSQL DB only. Refuses to run against any other DB."""
import cProfile, io, os, pstats, statistics, sys, time
from collections import defaultdict
from pathlib import Path

assert os.environ.get("INAZINA_PG_DSN") == "postgresql:///inazina_profile_20261121", "wrong DSN"
assert os.environ.get("INAZINA_DB_BACKEND") == "postgres"
ROOT = Path.home() / "Desktop/inazina"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from database import DatabaseManager
from managers.market_manager import MarketManager
from managers import price_backend

print("price backend:", price_backend.BACKEND)
db = DatabaseManager()
market = MarketManager(db)

now = int(time.time())
t0 = time.perf_counter()
market.update_prices(now=now)
first = (time.perf_counter() - t0) * 1000
print(f"first tick (includes catch-up since backup): {first:.1f} ms")

timings = []
prof = cProfile.Profile()
for i in range(1, 21):
    t0 = time.perf_counter()
    prof.enable()
    market.update_prices(now=now + i * 5)
    prof.disable()
    timings.append((time.perf_counter() - t0) * 1000)
timings.sort()
print(f"steady ticks x20 (cProfile on, so inflated): median {statistics.median(timings):.1f} ms, p95 {timings[18]:.1f} ms, max {timings[-1]:.1f} ms")

plain = []
for i in range(21, 41):
    t0 = time.perf_counter()
    market.update_prices(now=now + i * 5)
    plain.append((time.perf_counter() - t0) * 1000)
plain.sort()
print(f"steady ticks x20 (no profiler): median {statistics.median(plain):.1f} ms, p95 {plain[18]:.1f} ms, max {plain[-1]:.1f} ms")

stats = pstats.Stats(prof)
total = stats.total_tt
by_file = defaultdict(float)
for (fn, line, name), (cc, nc, tt, ct, callers) in stats.stats.items():
    if "/Desktop/inazina/" in fn:
        key = fn.split("/Desktop/inazina/")[1]
    elif "site-packages" in fn:
        key = "site-packages/" + fn.split("site-packages/")[1].split("/")[0]
    elif fn.startswith("~") or fn.startswith("<"):
        key = "builtins/C"
    else:
        key = "stdlib/" + os.path.basename(fn)
    by_file[key] += tt
print(f"\nself-time by file (profiled total {total*1000:.0f} ms over 20 ticks):")
for k, v in sorted(by_file.items(), key=lambda kv: -kv[1])[:14]:
    print(f"  {v/total*100:5.1f}%  {v*1000:8.1f} ms  {k}")

s = io.StringIO()
pstats.Stats(prof, stream=s).sort_stats("cumulative").print_stats(22)
print("\ntop cumulative:")
print("\n".join([l for l in s.getvalue().splitlines() if l.strip()][:34]))
```

### B. `prof_sql.py` — 틱당 논리 문장 수, 실제 왕복 수, SQL 모양별 시간

```python
"""Count logical statements and raw round trips per tick, grouped by SQL shape. SCRATCH DB only."""
import os, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

assert os.environ.get("INAZINA_PG_DSN") == "postgresql:///inazina_profile_20261121", "wrong DSN"
assert os.environ.get("INAZINA_DB_BACKEND") == "postgres"
ROOT = Path.home() / "Desktop/inazina"
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)

from database import DatabaseManager, pg_backend
from managers.market_manager import MarketManager

logical = Counter(); logical_t = defaultdict(float); raw = Counter(); raw_t = defaultdict(float)
orig_exec = pg_backend.PgCursor.execute

def shape(sql):
    s = re.sub(r"\s+", " ", sql.strip())
    s = re.sub(r"'[^']*'", "'?'", s)
    return s[:90]

def timed_execute(self, sql, parameters=()):
    key = shape(sql)
    t0 = time.perf_counter()
    r = orig_exec(self, sql, parameters)
    logical[key] += 1; logical_t[key] += time.perf_counter() - t0
    return r
pg_backend.PgCursor.execute = timed_execute

import psycopg
orig_raw = psycopg.Cursor.execute
def raw_execute(self, query, params=None, **kw):
    key = shape(query if isinstance(query, str) else str(query))
    key = "SAVEPOINT/RELEASE/ROLLBACK" if key.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO")) else "(statement)"
    t0 = time.perf_counter()
    r = orig_raw(self, query, params, **kw)
    raw[key] += 1; raw_t[key] += time.perf_counter() - t0
    return r
psycopg.Cursor.execute = raw_execute

db = DatabaseManager(); market = MarketManager(db)
now = int(time.time())
market.update_prices(now=now)            # warm-up / catch-up
logical.clear(); logical_t.clear(); raw.clear(); raw_t.clear()
N = 5
t0 = time.perf_counter()
for i in range(1, N + 1):
    market.update_prices(now=now + i * 5)
wall = (time.perf_counter() - t0) * 1000 / N
print(f"wall per tick: {wall:.0f} ms (N={N})")
print(f"logical statements per tick: {sum(logical.values())/N:.0f}   raw round trips per tick: {sum(raw.values())/N:.0f}")
for k in raw:
    print(f"  raw {k}: {raw[k]/N:.0f}/tick, {raw_t[k]*1000/N:.0f} ms/tick")
print("\ntop logical statements by total time (per tick):")
for k, v in sorted(logical_t.items(), key=lambda kv: -kv[1])[:14]:
    print(f"  {v*1000/N:7.1f} ms  x{logical[k]/N:6.0f}  {k}")
```

### C. 동일성 비교 스케치 (`base` vs `new`, 구현 세션이 완성)

```python
# 같은 now와 시드로 두 DB에서 같은 틱 수를 돌린 뒤 테이블을 전부 비교한다.
# for label, dsn in (("base", "postgresql:///inazina_profile_base"), ("new", "postgresql:///inazina_profile_new")):
#     (프로세스를 나눠 INAZINA_PG_DSN을 바꿔 실행; 한 프로세스에서 DSN을 바꾸면 DatabaseManager가 섞일 수 있다)
#     random.seed(42 + tick); market.update_prices(now=now + tick)
# 비교 대상 표: market_assets, company, investment, market_simulation_state, market_auto_targets,
#   market_price_history, market_exchanges, market_exchange_index_history  (각각 SELECT * ... ORDER BY 1)
# 한 표라도 다르면 실패.
```


## 11. 실행 결과 (2026-11-21)

**한 일**: A(`PgCursor.executemany`를 배치 단위로 교체)와 B(루프마다 거래소 상태를 한 번만 조회). 코드는 `database/pg_backend.py`, `managers/market_manager.py`, 시험은 `tests/test_pg_executemany.py`, 측정 도구는 `tools/profile_market_tick.py`다.

| 항목 | 기준 코드 | 수정 코드 |
|---|---|---|
| 틱 중앙값 (부하 낮음, 번갈아 3회) | 109ms | **61ms** (−44%) |
| 틱당 실제 왕복 수 | 1,684 | **291** (−83%) |
| 표 내용 | — | 아래 9개 표 해시가 **전부 일치** |

일치를 확인한 표: `market_assets`, `company`, `investment`, `market_simulation_state`, `market_auto_targets`, `market_price_history`, `market_exchanges`, `market_exchange_index_history`, `market_exchange_index_components`. 같은 시드·같은 시각으로 첫 틱과 6틱을 돌린 뒤 비교했다. 전체 시험은 1364개 통과이고, 남은 실패 3개는 이 변경과 무관하다(다른 세션의 작성 중인 변경이며, 두 변경을 되돌려도 똑같이 실패한다).

**A의 설계**
- 트랜잭션 안에서 `SAVEPOINT` 하나, psycopg `executemany`(파이프라인) 하나, `RELEASE` 하나로 보낸다. 행당 왕복 3번이 배치당 3번이 된다.
- 배치가 실패하면 배치를 되돌리고 **행 단위로 다시 실행**한다. 그래서 실패한 행 앞의 행들이 반영된 채 남고 오류 종류도 예전과 같다(오류를 잡고 계속하는 호출자의 동작이 바뀌지 않는다). `tests/test_pg_executemany.py`가 이를 고정한다.
- 트랜잭션 밖(자동 커밋)에서는 예전처럼 행마다 보낸다.
- `executemany` 뒤에 `lastrowid`를 쓰는 호출자는 없다(호출자는 `rowcount`만 쓴다). 그래서 `RETURNING`을 붙이지 않는다.

**B의 설계**: 문서의 `_row` 캐시 대신, 더 작고 안전한 방법을 썼다. 가격을 매기는 루프 안에서는 거래소 상태가 바뀌지 않으므로, 루프마다 거래소별 답을 한 번만 받아 재사용한다(`MarketManager._session_blocked`). 연결 전체에 캐시를 두면 서킷브레이커처럼 틱 중간에 바뀌는 상태를 놓칠 수 있어서 피했다.

**C(읽기 문장의 savepoint)는 하지 않는다.** PostgreSQL은 실패한 문장이 트랜잭션 전체를 중단시켜서, 읽기 오류를 잡고 계속하는 호출자가 하나라도 있으면 안전하지 않다. 남은 왕복 291번 중 savepoint가 약 2/3이지만 틱이 61ms로 내려와서 얻을 것이 작다.

**측정에서 배운 것**
- **측정이 비결정적이면 동일성 검사를 못 한다.** 같은 코드를 두 번 돌려도 표 해시가 달랐다. 원인은 틱 시각을 실제 시각보다 **미래**로 준 것이었다(엔진이 실제 시계로 구간을 잘라 실행마다 달랐다). 과거 시각으로 바꾸고 `PYTHONHASHSEED=0`을 고정하자 같은 코드 두 번의 해시가 일치했다.
- **부하에 따른 절대 시간 차이가 크다.** 같은 코드가 부하 평균 16~24에서는 460~1300ms, 부하 4에서는 109ms로 나왔다. 앞의 809ms~1.5초도 부하가 높을 때의 값이었다. 판정은 번갈아 잰 비율로 해야 한다.
- 기준선의 틱당 왕복 수는 시각에 따라 1,684~2,684로 달랐다(처리되는 자산 수가 다르다).

**아직 안 한 것**: 4.5(주문 체결·웹 API·은행 이자 등 다른 시나리오 측정), 4.4(남은 61ms의 파이썬 계산 분석). 스크래치 DB `inazina_profile_*`는 끝나면 `dropdb`로 지운다.
