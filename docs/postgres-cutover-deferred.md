# PostgreSQL 전환 시 처리할 보류 항목

PostgreSQL을 운영 DB로 전환할 때(`INAZINA_DB_BACKEND=postgres`) 반드시 다시 확인하고 처리한다.
의도적으로 미룬 항목이며 누락이 아니다. (2026-11-18 기준, PR #123 병합 이후)

현재 상태: 테스트는 SQLite 전체 통과, PostgreSQL 전체 통과(`SQLITE_ONLY` 제외).

## 1. 금액 스냅샷 점검은 PostgreSQL에서 원본 직접 점검으로 대체했다
- `financial_amount_snapshots`는 PostgreSQL에 싣지 않는다(`DROPPED`). PostgreSQL의 금액은
  정확한 NUMERIC이며 필드 등록부와 면제 목록은 이관한다.
- `audit_amount_snapshots`는 PostgreSQL에서 등록된 원본 필드의 존재와 모든 NUMERIC `*_units` 열의 소수 단위를 직접 점검한다
  (`tests/test_pg_amount_audit.py`). 삭제된 행의 스냅샷이나 미등록 열 후보 점검은 SQLite 전용이다.
- `audit_amount_field_coverage`는 이관된 등록부와 면제 목록을 사용해 미분류 금액 필드를 확인한다.
  기존 PostgreSQL DB에는 현재 코드로 초기화한 SQLite DB 사본을 사용해
  `tools/migrate_postgres_amount_registry.py`를 실행해야 한다. 등록부가 없으면 정상으로 처리하지 않는다.

## 2. ELS 이전 시 중복 상품 처리
금리 이력, 상품 등록 및 회생·파산 정책의 REPLACE 위험은 명시적인 갱신으로 처리했다.
ELS 이전은 기존 상품과 이전 데이터의 코드가 겹치면 자동 이전을 중단하고 양쪽 데이터를
보존한다. 중복이 있으면 운영자가 내용과 청약을 확인해 정리한 뒤 이전을 다시 실행해야 한다.
세부 내용은 `docs/postgres-replace-audit.md`를 참고한다.

## 3. `SQLITE_ONLY` 테스트는 PostgreSQL 커버리지가 없다 (2026-11-20: SQLite 지원 종료로 항상 건너뜀, 지우지는 않음)
`tests/conftest.py`의 `SQLITE_ONLY` 목록(이유가 각 항목에 적혀 있다). 전환 전에 목록을 다시 검토해,
PostgreSQL용 동등 테스트가 필요한 항목을 골라낸다.
- SQLite 파일을 직접 열거나 잠그는 테스트(동시 쓰기, 무결성 감사)
- 구형 SQLite 데이터 마이그레이션 테스트(PostgreSQL은 현재 스키마에서 시작)
- 간이 SQLite 표를 직접 만들어 검사하는 테스트(`test_web_api.py` 등)

## 4. 알려진 동작 차이
- PostgreSQL의 FK는 항상 강제된다. SQLite 테스트 연결은 FK가 꺼져 있어 통과하던 삭제가 PostgreSQL에서는 막힌다
  (예: 국고 계정 사용자 삭제 전에 `country_treasuries` 행을 먼저 지워야 한다).
- `DatabaseManager()`를 다시 만들어도 PostgreSQL에서는 SQLite 시작 마이그레이션이 돌지 않는다.
