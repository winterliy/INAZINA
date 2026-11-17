# PostgreSQL 전환 시 처리할 보류 항목

PostgreSQL을 운영 DB로 전환할 때(`INAZINA_DB_BACKEND=postgres`) 반드시 다시 확인하고 처리한다.
의도적으로 미룬 항목이며 누락이 아니다. (2026-11-18 기준, PR #123 병합 이후)

현재 상태: 테스트는 SQLite 전체 통과, PostgreSQL 전체 통과(`SQLITE_ONLY` 제외).

## 1. 금액 스냅샷 점검은 PostgreSQL에서 원본 직접 점검으로 대체했다
- `financial_amount_snapshots`, 필드 등록부, 면제 목록은 PostgreSQL에 싣지 않는다(`DROPPED`). PostgreSQL의 금액은
  정확한 NUMERIC이라 SQLite의 정수 복사본이 필요 없다.
- `audit_amount_snapshots`는 PostgreSQL에서 모든 NUMERIC `*_units` 열에 소수 단위가 섞였는지 원본 표를 직접 점검한다
  (`tests/test_pg_amount_audit.py`). 삭제된 행의 스냅샷이나 미등록 열 후보 점검은 SQLite 전용이다.
- `audit_amount_field_coverage`는 여전히 `skipped`로 이유를 남긴다. 전환 시 운영 화면에도 이 차이를 알린다.

## 2. `INSERT OR REPLACE` 위험 4가지
`docs/postgres-replace-audit.md`에 기록돼 있다. 코드는 고치지 않았다.
- 금리 이력 트리거: SQLite는 INSERT, PostgreSQL은 UPDATE 이벤트로 기록해 원장 대조 기준이 다르다.
- ELS 상품 이전: 청약이 있으면 불변 트리거가 거부할 수 있다.
- 저축·대출 등록 헬퍼: `bank_products`에서 같은 코드를 공유해 서로 덮을 수 있다.
- 회생·파산 정책 REPLACE.

## 3. `SQLITE_ONLY` 테스트는 PostgreSQL 커버리지가 없다
`tests/conftest.py`의 `SQLITE_ONLY` 목록(이유가 각 항목에 적혀 있다). 전환 전에 목록을 다시 검토해,
PostgreSQL용 동등 테스트가 필요한 항목을 골라낸다.
- SQLite 파일을 직접 열거나 잠그는 테스트(동시 쓰기, 무결성 감사)
- 구형 SQLite 데이터 마이그레이션 테스트(PostgreSQL은 현재 스키마에서 시작)
- 간이 SQLite 표를 직접 만들어 검사하는 테스트(`test_web_api.py` 등)

## 4. 알려진 동작 차이
- PostgreSQL의 FK는 항상 강제된다. SQLite 테스트 연결은 FK가 꺼져 있어 통과하던 삭제가 PostgreSQL에서는 막힌다
  (예: 국고 계정 사용자 삭제 전에 `country_treasuries` 행을 먼저 지워야 한다).
- `DatabaseManager()`를 다시 만들어도 PostgreSQL에서는 SQLite 시작 마이그레이션이 돌지 않는다.
