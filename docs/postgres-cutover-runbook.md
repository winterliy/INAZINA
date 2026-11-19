# PostgreSQL 운영 전환 절차

운영 DB를 SQLite(`database/inazina.db`)에서 PostgreSQL로 옮기는 순서입니다. 서비스를 멈춰야 하며, 실행 직전에 사용자 확인을 받습니다.
리허설은 운영 복사본(2026-11-19, 644,731행, 252개 표)으로 통과했습니다: 행 수·금액 합계 일치, 통합 대사 `matched`, 이상 0건.

## 준비 (서비스 중지 전)
1. 다른 세션의 PostgreSQL 관련 미커밋 변경(`functions/money.py`, `database/pg_backend.py` 등)이 커밋되었는지 확인한다.
2. `.venv/bin/python tools/pg_cutover_check.py` 사용법은 파일 머리말을 따른다.
3. PostgreSQL 17이 떠 있는지(`brew services list`) 확인한다. 운영용 빈 DB를 만든다: `createdb inazina`.

## 전환 (서비스 중지 시간 시작)
1. 디스코드 봇, 웹 API, 대시보드·hts 서버를 모두 끈다. 사건 재등록 감시 프로세스가 있으면 함께 끈다.
2. SQLite를 백업한다: `sqlite3 database/inazina.db ".backup 'database/backups/inazina-before-pg-<날짜>.db'"`.
3. 백업본을 최신 코드로 초기화해 마이그레이션을 적용한다(스크립트: `DBManager.database_dir`을 백업 폴더로 지정한 뒤 `DatabaseManager().close()`).
4. 적재한다: `.venv/bin/python tools/sqlite_to_postgres.py <백업본>.db "postgresql:///inazina"` (대상의 `public` 스키마를 지우고 다시 만든다).
5. 검증한다: `.venv/bin/python tools/pg_cutover_check.py <백업본>.db "postgresql:///inazina"` 가 `OK`여야 한다. 하나라도 다르면 여기서 멈추고 SQLite로 계속 쓴다.
6. `.env`에 `INAZINA_DB_BACKEND=postgres`, `INAZINA_PG_DSN=postgresql:///inazina` 두 줄을 넣는다.
7. 서비스를 켠다(웹 API, 대시보드, 봇). 로그인, 내 계정, 거래, 유동성 탭, 통합 대사를 스모크 확인한다.

## 되돌리기
- 전환 직후(쓰기가 거의 없을 때): `.env`의 두 줄을 빼고 서비스를 다시 켜면 SQLite로 돌아간다. SQLite 파일은 건드리지 않았다.
- PostgreSQL로 운영한 뒤에 되돌리면 그 사이의 쓰기는 SQLite에 없다. 그 경우는 먼저 `pg_dump`로 보존하고 사용자와 상의한다.

## 전환 뒤
- 봇의 하루 1회 백업은 PostgreSQL에서 `pg_dump`(custom 형식, `database/backups/inazina-pg-*.dump`, 최근 14개 보관)를 만든다. 복원은 `pg_restore`. 수동 실행: `INAZINA_DB_BACKEND=postgres python -m database.backup`.
- `run.py`가 `.env`를 읽어 봇·웹 API·도구가 같은 백엔드를 쓰게 한다(시작할 때 `Database backend: ...`를 출력한다).
- 금액 스냅샷 점검은 PostgreSQL에서 원본 표를 직접 점검하는 방식이다. 화면·문서에 이 차이를 알린다.
- 정리: 리허설용 DB `inazina_rehearsal`, `inazina_dev`를 삭제한다.

## 새 표·컬럼 반영 (전환 뒤 개발 규칙)
- PostgreSQL은 시작할 때 `database/pg_schema_sync.py`가 현재 코드가 정의하는 레이아웃(코드가 만든 빈 SQLite를 `tools/build_postgres_schema.py`로 변환한 것)과 운영 DB를 비교해 **없는 표·컬럼·인덱스·뷰·트리거만 추가**한다. 기존 표와 데이터는 건드리지 않는다.
- 스키마를 정의하는 코드(`database/`, `managers/`, `web_api/`, `functions/`)의 지문이 바뀐 시작에서만 실행되고(약 20초), 같으면 쿼리 한 번으로 끝난다. 실패한 문장은 `[PostgreSQL schema sync] could not apply ...`로 출력되고 서버는 계속 뜬다.
- 미리 보기: `INAZINA_DB_BACKEND=postgres python -m database.pg_schema_sync --dry-run`. 끄기: `INAZINA_PG_SCHEMA_SYNC=0`. 테스트(pytest)에서는 꺼져 있다.
- 한계: NOT NULL이고 기본값이 없는 새 컬럼은 기존 행 때문에 NULL 허용으로 추가하고, 이름이 없는 외래키는 새로 만들어진 표에만 붙는다. 컬럼 삭제·타입 변경·이름 변경은 자동으로 하지 않으니 수동 마이그레이션이 필요하다.

## 전환 후 재검토 결과 (2026-11-19)

전환 때 다시 확인하기로 한 항목을 운영 PostgreSQL에서 점검한 결과다.

- **금액 스냅샷 점검**: 건너뛰지 않는다. PG에서는 원천 테이블의 NUMERIC `*_units` 소수 검사로 바뀌었고, 통합 대조의 `amount_field_coverage`는 등록 167·면제 20·미분류 0으로 `matched`다.
- **REPLACE 위험 4건**: 모두 명시적 충돌 갱신으로 고쳤다(`docs/postgres-replace-audit.md` 처리 결과 참고).
- **`SQLITE_ONLY` 45개**: 24개는 옛 SQLite 스키마를 올리는 이전 시험이라 PG에는 해당이 없다(PG 스키마는 새로 생성한다). 9개는 줄인 SQLite 표를 직접 만드는 시험, 4개는 쿼리 계획·인덱스 시험, 8개는 기타다. 줄인 표 시험과 쿼리 계획 시험은 PG용(`EXPLAIN`, 실제 스키마)으로 옮길 만하다. "스냅샷 재설계 대기"라고 적힌 4개는 위 점검으로 이미 대체돼서 사유를 고쳐야 한다.
- **읽기 일관성**: PG 읽기 전용 연결은 `REPEATABLE READ`로 연다. 기본 격리 수준에서는 LP가 호가를 바꾸는 동안 통합 대조가 주문을 한 문장에서는 열림, 다음 문장에서는 취소로 읽어 일시적으로 `mismatch`가 났다. SQLite 읽기와 같은 단일 스냅샷이 맞다.
- **SQLite 파일을 읽는 도구**: `tools/repair_ledger.py`는 PG로 운영 중이면 실행을 거부한다(`--sqlite-file`로 의도했을 때만 허용). `sqlite_to_postgres.py`와 `build_postgres_schema.py`는 SQLite 원본이 필요한 도구라 그대로 둔다. `benchmark_price_native.py`는 백업을 읽기 전용으로 쓴다.
- **개발용 DB**: `inazina_dev`(2026-11-01 이후 사용 없음)와 `inazina_schema`는 코드·문서가 이름을 참조해서 지우지 않았다. `inazina_test_*`는 시험 실행이 정상 종료되지 않았을 때 남는 임시 DB라 시험이 끝난 뒤 지운다.
- **PR #123**: 병합 커밋 `aba8781`이 현재 master에 이미 들어 있어 따로 맞출 것이 없다.

## 옛 SQLite 파일 보관 (2026-11-19)

전환 시점의 SQLite 파일(`inazina.db`, `-wal`, `-shm`)은 `database/backups/sqlite-frozen-20261119/`로 옮겼다. 실수로 SQLite로 켜지는 일을 막기 위해서다. 세 파일은 함께 보관해야 한다. 되돌릴 때는 세 파일을 `database/`로 복사하고 `.env`의 `INAZINA_DB_BACKEND`/`INAZINA_PG_DSN` 두 줄을 지운다. 그 사이에 PostgreSQL에 쌓인 쓰기는 SQLite에 없으므로 먼저 `pg_dump`로 보존한다.
