# PostgreSQL 백업 복원 연습

`python tools/backup_restore_drill.py /path/to/backup.dump --require-table users --require-table accounts --reconcile`은 PostgreSQL custom-format 백업을 `INAZINA_PG_DSN`이 가리키는 서버의 임시 `inazina_drill_*` 데이터베이스에 복원한다. 별도의 격리된 테스트 서버와 DB 생성·삭제 권한이 필요하다. 신뢰할 수 있는 백업만 사용한다. 복원은 백업의 SQL을 실행할 수 있다.

`--require-table`은 반복할 수 있으며 지정한 public 테이블의 존재를 검사한다. 외래키 검사는 검증되지 않은 제약을 보고하며 모든 데이터와 업무 참조를 독립적으로 재검증하지 않는다. `--sync-schema`는 복원본(임시 DB)에 현재 코드의 스키마 동기화를 적용해, 코드보다 오래된 백업에 새 표가 없어 대사가 실패하는 일을 막는다. 데이터 행(예: 금액 면제 등록부)은 옮기지 않는다. `--reconcile`은 복원본에서 구현된 통합 금융 대사를 읽기 전용으로 실행하고 차이 또는 미연결 원장이 있으면 실패한다.

JSON의 `ok`가 참이고 종료 코드가 0이면 요청된 검사와 임시 DB 삭제가 성공한 것이다. `cleanup_ok`가 거짓이면 임시 DB가 남았을 수 있으므로 `restore_database`를 확인해 관리자가 정리해야 한다. `elapsed_seconds`는 이 도구의 복원·검사 시간이며 서비스 전체 복구 시간이 아니다. 복원본에는 민감한 데이터가 있을 수 있으므로 테스트 서버의 접근·보존 정책을 적용한다.

API 및 작업자 기동, 백업 최신성, 모든 업무 참조와 실서비스 복구 시간은 검증하지 않는다. API·작업자 훈련은 운영 DB 및 외부 연결이 차단된 환경에서 별도로 수행한다. `tools/verify_backup.py`의 SQLite 검사는 폐기됐다.
