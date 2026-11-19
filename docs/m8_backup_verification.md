# SQLite 백업 점검

`python tools/verify_backup.py /path/to/backup.db`는 지정된 기존 SQLite 백업을 읽기 전용으로 열고 SQLite `integrity_check`와 선언된 외래키를 검사한다. JSON의 `ok`가 참이면 종료 코드는 0이고 검사 오류 또는 차이는 1이다. 없는 파일, 빈 파일과 SQLite 형식이 아닌 파일은 실패한다. 프로젝트의 CHECK 제약에 쓰이는 함수를 등록한다.

운영 DB 대신 `python -m database.backup` 등으로 만든 완결된 백업 파일을 대상으로 실행한다. 출력에 외래키 위반 테이블과 행 식별자가 포함될 수 있으므로 접근을 제한한다. 이 검사는 물리적 무결성과 선언된 관계만 확인하며 백업의 최신성, 모든 업무 참조, 원장 대사, 복원 가능 서비스 환경이나 실제 복구 훈련을 보장하지 않는다. 별도 격리 환경에서 복원·서비스 기동·업무 대사를 점검해야 한다. PostgreSQL 백업 검증에는 사용할 수 없다.

## 격리 복구 리허설

`python tools/backup_restore_drill.py /path/to/backup.db`는 백업을 임시 디렉터리의 새 SQLite 파일로 복원한 다음 위 검증을 실행하고 임시 파일을 정리한다. 원본을 SQLite immutable 모드로 읽으므로 완결된 단일 백업 파일만 사용해야 한다. 실행 중인 DB나 WAL 또는 롤백 저널에 의존하는 사본은 입력하지 않는다. `-wal` 또는 `-journal` 사이드카가 있으면 검사를 거부한다. 종료 코드 0은 복사와 이 제한된 검사 성공을 뜻한다. JSON `elapsed_seconds`는 이 도구의 복사·검사 경과 시간이며 서비스의 전체 복구 시간은 아니다. 서비스 기동, 백업 최신성 확인, 업무 대사 및 전체 복구 시간 측정은 별도로 해야 한다. 민감한 복구 사본이 임시 디렉터리에 생성되므로 해당 시스템의 임시 저장소 접근 통제와 보존 설정을 확인한다.


## 필수 스키마 확인

두 명령에 `--require-table users`처럼 옵션을 반복해 필수 테이블 이름을 전달하면 정확히 일치하는 테이블이 없을 때 실패하며 JSON `missing_tables`에 표시한다. 뷰는 테이블로 인정하지 않는다. 복구 리허설은 복원된 사본에서 확인한다. 예: `python tools/backup_restore_drill.py /path/to/backup.db --require-table users --require-table accounts`. 운영자가 사용하는 스키마와 버전에 맞춰 목록을 정해야 한다. 이 검사는 열·행 내용이나 업무 원장 대사를 확인하지 않으며, 옵션을 생략하면 테이블 존재를 검사하지 않는다.

## 복원본 업무 대사

`python tools/backup_restore_drill.py /path/to/backup.db --reconcile`은 무결성 검사를 통과한 임시 복원본을 SQLite 읽기 전용·`query_only`로 열어 `AccountingJournal.reconcile_all_financial_ledgers`를 실행한다. 결과의 `reconciliation`에는 기존 검사 범위, finding 수, 미연결 표, 마감 준비 여부가 포함된다. 대사 차이가 있거나 미연결 표가 남아 마감 준비가 되지 않았다면 종료 코드 1이다. 필요한 테이블이 없거나 검사 오류가 나도 실패한다. 이 대사는 현재 구현된 검사에 한정된다.

이 리허설은 네트워크 서비스에 접속하거나 API/작업자를 기동하지 않는다. 실제 서비스·작업자 기동 검증은 외부 연결과 운영 데이터 접근이 차단된 별도의 격리 환경에서 수행해야 한다. 업무별 복원과 사용자 흐름을 검증한 것으로 간주하지 않는다.
