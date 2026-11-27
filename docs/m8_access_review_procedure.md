# 관리자 접근 정기 검토 절차

주기: 매월(35일을 넘기면 '통제 · 복구' 화면이 경고한다). 검토자는 운영자 본인이다.

1. `python tools/access_review.py`로 관리자 목록과 최근 30일 감사 요약을 본다(읽기 전용 조회).
2. 확인한다: 더 이상 필요 없는 관리자가 있는가, 처음 보는 행위자나 평소와 다른 행위 수가 있는가, 감사 기록이 끊긴 구간이 있는가(`admin_audit`의 범위).
3. 결과를 `--append`로 `docs/m8_records/drills.json`의 `access_reviews`에 추가하고 `findings`를 사람이 채운다. 기록은 추가만 하고 고치지 않는다.
4. 변경이 필요하면(관리자 해임 등) 별도 작업으로 하고 그 사실을 `findings`에 적는다.
5. 착륙은 `tools/land_commit.py`로 한다.

환경: 부트스트랩 관리자 ID는 `INAZINA_BOOTSTRAP_ADMIN_IDS`(쉼표 구분)로 바꾼다. 비우면 기본값(기존 계정)을 쓴다.
백업 점검: 가장 최근 `database/backups/inazina-pg-*.dump`가 48시간(`INAZINA_BACKUP_MAX_AGE_HOURS`)보다 오래되면 화면에 '오래됨', 없으면 '없음'으로 표시한다. 점검은 읽기 전용이며 백업을 만들지 않는다.

서버 재시작·큰 반영 뒤에는 접근 검토와 별도로 `docs/m8_prod_migration_runbook.md`의 '정기 점검'(복원본 `--reconcile`이 matched인지)을 한다.
