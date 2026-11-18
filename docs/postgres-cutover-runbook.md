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
