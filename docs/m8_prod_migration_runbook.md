# 운영 반영 절차서 (문서만 — 실행 전에 사용자 승인)

2026-11-25 작성. 이 문서의 명령 중 **운영 DB에 쓰는 것은 3절의 두 가지(서버 재시작으로 생기는 스키마 동기화, 금액 등록부 이관)뿐**이다. 나머지는 읽기 전용이거나 복사본에서만 돈다. 각 절은 복사본으로 한 번 리허설했고(아래 '리허설 기록') 운영에는 실행하지 않았다. 운영 DB 이름은 `inazina`, 복사본은 `inazina_<이름>`처럼 다른 이름을 쓴다.

## 1. 사전: 백업과 복원 확인
```
INAZINA_PG_DSN=postgresql:///inazina .venv/bin/python -m database.backup      # database/backups/inazina-pg-<시각>.dump 를 만든다(pg_dump, 읽기 전용)
.venv/bin/python tools/backup_restore_drill.py database/backups/inazina-pg-<시각>.dump --require-table users --require-table accounts --expect-dbname inazina
```
**DSN을 반드시 적는다.** `INAZINA_PG_DSN`이 없으면 백업 도구의 기본값은 개발 DB `inazina_dev`다(2026-11-27에 실제로 겪음). 복원 점검은 백업 헤더의 `dbname`을 보고서 `source_dbname`에 적고, `--expect-dbname inazina`가 다른 DB의 덤프를 거부한다. 기대: `"ok": true`, `"source_dbname": "inazina"`, `foreign_key_violations: []`, `cleanup_ok: true`. 하나라도 아니면 중단한다. 이 덤프가 5절 롤백의 기준점이다. '통제 · 복구' 화면의 백업 최신성이 '정상'인지도 본다.

## 2. 서버 재시작이 만들 새 표 목록 (자동 추출)
서버는 시작할 때 `pg_schema_sync`로 코드의 스키마 정의를 PostgreSQL에 더한다(추가만 하고 지우지 않는다). 무엇이 더해질지는 **복사본에서** 미리 뽑는다.
```
createdb inazina_rehearsal && pg_restore --no-owner --no-privileges -d inazina_rehearsal database/backups/inazina-pg-<시각>.dump
INAZINA_PG_DSN=postgresql:///inazina_rehearsal .venv/bin/python tools/pg_schema_preview.py
```
`tools/pg_schema_preview.py`는 표·열·인덱스·뷰·제약·트리거 이름을 모두 JSON으로 출력하고(쓰기 없음), 운영 DB 이름 `inazina`는 `--allow-live` 없이는 거부한다. 같은 사전 점검을 2026-11-20 백업(`before-korean-names`)에 적용한 결과 새로 생길 것은 표 7개(`contract_rate_schedules`, `deposit_insurance_fund_events`, `ftp_rate_applications`, `fx_revaluation_corrections`, `jurisdiction_rate_rules`, `product_rate_schedules`, `prudential_parameters`), 인덱스 3개, 제약 1개, 트리거 10개였다. 2026-11-25의 운영 복사본에서는 모두 **0개**다(운영 서버가 이미 새 코드로 재시작되어 반영됨). 새 코드로 다시 재시작하기 전에는 이 명령으로 목록을 다시 뽑아 이 절에 날짜와 함께 적는다.

## 3. 금액 등록부 이관 (`tools/migrate_postgres_amount_registry.py`)
스키마 동기화는 표만 만들고 **데이터 행은 옮기지 않는다.** 금액 필드 등록부(`financial_amount_field_registry`)와 면제 등록부(`financial_amount_field_exemptions`)의 행은 현재 코드로 초기화한 SQLite 스키마 DB에서 복사한다.
```
# (a) 소스: 현재 코드로 초기화한 SQLite 스키마 DB를 임시 폴더에 만든다 (운영과 무관)
INAZINA_DB_BACKEND=sqlite-schema .venv/bin/python - <<'PY'
from pathlib import Path
from database.db_manager import DBManager
from database import DatabaseManager
folder = Path('/tmp/inazina-registry-src'); folder.mkdir(parents=True, exist_ok=True)
DBManager.database_dir = folder
DatabaseManager().close()
PY
# (b) 이관 (한 트랜잭션, 여러 번 돌려도 같은 결과)
.venv/bin/python tools/migrate_postgres_amount_registry.py /tmp/inazina-registry-src/inazina.db postgresql:///inazina
```
기대 출력: `금액 필드 167개, 면제 23개 이관 완료`(2026-11-25 코드 기준. 코드가 바뀌면 수가 바뀐다). 운영에서는 먼저 복사본(`postgresql:///inazina_rehearsal`)에 같은 명령을 돌려 출력이 같은지 확인한 뒤 운영 대상으로 바꾼다. 효과: 기존에 없던 면제 행(`insolvency_policies.exempt_amount`)이 더해진다. 이 행이 없으면 4절의 통합 대사가 `mismatch`(발견 1건)로 나온다. 이미 있는 행은 같은 값으로 갱신되고 운영에만 있는 행은 지우지 않는다.

## 4. 이관 후 확인
```
INAZINA_PG_DSN=postgresql:///inazina .venv/bin/python -m database.backup
.venv/bin/python tools/backup_restore_drill.py database/backups/inazina-pg-<새 시각>.dump --require-table users --require-table accounts --reconcile --sync-schema --expect-dbname inazina
```
기대: `"ok": true`, `reconciliation.status: "matched"`, `close_ready: true`, `finding_count: 0`, `cleanup_ok: true`. 발견이 있으면 `findings`에 원인이 나온다. 통과하면 `docs/m8_records/drills.json`에 날짜와 함께 기록한다.

## 5. 롤백
- 스키마 동기화: 추가만 한다. 새 표는 비어 있고 쓰이지 않으면 그대로 둬도 안전하다. 완전히 되돌리려면 1절 덤프로 복원한다.
- 금액 등록부 이관: 이관 전 면제 행 목록을 저장해 두었다가(`\copy financial_amount_field_exemptions TO ...`) 이관으로 더해진 행만 지운다. 예: `DELETE FROM financial_amount_field_exemptions WHERE source_table='insolvency_policies' AND amount_field='exempt_amount';` 지우면 통합 대사가 다시 `mismatch`가 되는 것이 정상이다(등록부는 시험·점검용 목록이고 거래 데이터가 아니다).
- 전체 되돌림: 서버를 멈추고 1절 덤프를 새 DB에 `pg_restore`한 뒤 DSN을 바꿔 확인한다. 기존 DB는 삭제하지 말고 이름을 바꿔 보관한다. 2026-11-20 이후 SQLite로는 되돌릴 수 없다.

## 6. 기본 꺼짐 기능을 켜는 순서
모두 사용자 결정이고 각 단계 사이에 '통제 · 복구'와 마감 게이트가 이상 없는지 본다. 되돌릴 수 있는 것부터 켠다.
1. **계약일 규칙(M7)**: 관리자 화면 '금리 · 한도 · 정리 > 국가별 규칙'에서 규칙을 등록한다. 규칙이 없으면 기존 국가 `legal_max_rate`를 그대로 쓰므로 규칙을 등록하는 것이 곧 켜는 일이다. 규칙은 추가 전용이라 잘못 넣으면 새 규칙으로 덮어 쓴다(삭제 불가). 신규 대출·연체이자·재산정·연장에만 적용되고 기존 계약은 바뀌지 않는다.
2. **FTP 미리보기 값 확인(M6)**: '자금 · ALM'의 FTP 미리보기는 조회 전용이다. 조달 곡선과 유동성 프리미엄(기본 0.30%)으로 계산된 마진이 상식적인지 숫자를 확인한다. 바뀌는 것은 없다.
3. **자동 만기 처리, 은행 1곳**: '관할 기준'에서 `funding_auto_maturity_flag`를 1로 저장한다(확인창 경고, 감사 `prudential_parameter.set`). 켜기 전에 그 관할 은행의 만기 도래 도매조달과 상환 이자가 자본에 미치는 영향을 도매조달 탭에서 본다. 한 관할(한 은행)만 켜고 10분 주기 뒤 `funding.auto_process_due` 감사 기록과 은행 원장 대사를 확인한 다음 넓힌다. 끄려면 0으로 저장한다.
4. **FTP 적용·금리 일정**: `ftp_apply_enabled`를 1로 한 뒤 FTP 미리보기에서 상품 하나에 '적용'한다(감사 `ftp_rate.apply`, 되돌림 `ftp_rate_revert`은 스위치가 꺼져도 가능). 금리 일정은 '상품별 금리 일정'에서 상품 하나를 켜고 새 계약만 영향을 받는지 본다. 기존 계약에 `contract_rate` 스냅샷이 없는 상품은 FTP 적용이 거부된다.

## 리허설 기록 (복사본, 2026-11-25)
운영 읽기 전용 `pg_dump`(6.6MB)를 복사본 DB `inazina_m8_rehearsal`에 복원해 1~4를 한 번씩 돌렸다. 복사본 DB와 임시 파일은 끝나면 삭제했다. 운영 DB에는 쓰지 않았다.
| 절 | 결과 |
|---|---|
| 1 | 복원 점검 `ok: true`, 표 269개, 외래키 위반 0, 7.2초, 임시 DB 삭제 확인 |
| 2 | 운영 복사본에서 새 표 0개(이미 반영됨). 2026-11-20 백업에 같은 명령을 적용하면 표 7·인덱스 3·제약 1·트리거 10개를 뽑음 |
| 3 | 소스 스키마 DB(등록부 167행, 면제 23행) → `금액 필드 167개, 면제 23개 이관 완료`. 복사본 면제 행 22 → 23. 같은 명령을 다시 돌려도 23으로 같음 |
| 4 | 이관 전: `mismatch`, 발견 1건(`insolvency_policies.exempt_amount`). 이관 후 덤프: `ok: true`, `matched`, `close_ready: true`, 발견 0건, 33.9초 |

## 운영 이관 실행 기록 (2026-11-27)
통합관리 세션이 3절을 운영(`postgresql:///inazina`)에 실행했다: 등록부 167행, 면제 23행(이관 전 면제 22행은 작업 임시 폴더에 백업). 이 문서의 작성자는 실행하지 않았고 아래는 보고받은 사실과 이후 복사본 점검 결과다.
| 항목 | 결과 |
|---|---|
| 이관 전 복사본 통합 대사 | 발견 6건 |
| 이관 후 | 발견 5건. `insolvency_policies.exempt_amount` 발견은 해소됨 |
| 남은 5건 (2026-11-27 운영 복사본, `--sync-schema --reconcile`, `source_dbname: inazina`, 36.5초, `cleanup_ok: true`) | 금액 필드 미분류 3건: `market_price_anchor.anchor_price`, `price_floor_events.from_price`, `price_floor_events.to_price`. 공통 분개 `common_journals`(customer): `posting_gaps` 6건(지갑 항목 45,465 = 공통 항목 45,465, `balance_gaps` 0). 기관 보조원장 `institution_subledgers`: 낙원시민은행 `customer_deposits` NWX, 계정 합 0, 분개 합 500, 차이 -500 |
남은 5건의 원인은 이 문서에서 조사하지 않았다(M4 담당이 조사 중). 새로 생긴 가격 안전장치 표 3개 열의 분류, 공통 분개 6건, 낙원시민은행 500은 각각 사실만 적는다.

## 운영 쓰기 기록 (2026-11-27, 사용자 승인 하에 통합관리 세션이 `postgresql:///inazina`에 실행)
이 문서의 작성자는 실행하지 않았고, 아래는 통합관리 세션의 보고를 그대로 옮긴 것이다. 운영 쓰기는 3건이다.
| # | 내용 | 결과 |
|---|---|---|
| 1 | 금액 등록부 이관 2차 | 면제 23 → 26행. 추가된 행: `market_price_anchor.anchor_price`, `price_floor_events.from_price`, `price_floor_events.to_price` |
| 2 | 낙원시민은행 탈퇴 시험 회원 계좌 `4094375218`의 누락된 `deposit_close` 은행 분개 1건 소급 추가 | 500 NWX, `entry_id` 78. 복사본에서 리허설한 뒤 운영에 실행 |
| 3 | 낙원 관할 `funding_auto_maturity_flag=1` 저장 | 행위자 `system:integration-manager`, 감사 `prudential_parameter.set`, `admin_audit` 2100. 현재 낙원 은행의 도매조달이 0건이라 동작하는 것은 없다. 봇 재시작 시 10분 주기에 반영된다 |
확인: 위 반영 뒤 운영 덤프 복사본(백업 `inazina-pg-20261127-192056.dump`, `source_dbname: inazina`)에 `--sync-schema --reconcile`을 돌린 결과 `matched`, `close_ready: true`, 발견 0건이다. 위 '남은 5건'은 모두 해소됐다.

## 정기 점검
서버 재시작이나 큰 반영(스키마·등록부·관할 기준 변경) 뒤마다 4절의 `--sync-schema --reconcile`을 돌려 `matched`, `close_ready: true`, 발견 0건인지 확인하고 `docs/m8_records/drills.json`에 날짜와 함께 기록한다. 발견이 있으면 그 사실을 기록하고 담당에게 넘긴다. 관리자 접근 검토는 `docs/m8_access_review_procedure.md`를 따른다.

