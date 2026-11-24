# 팀 운영 규칙 (여러 세션이 한 작업 폴더에서 동시에 일할 때)

2026-11-21 통합관리 세션이 M4·M6·M7·M8·D(시장 틱 개선)·E(다른 경로 병목) 하위 세션을 나눠 맡길 때 정한 규칙이다. 하위 세션과 이후 세션은 일하기 전에 읽는다.

## 구조
- **통합관리 세션**: 작업 배분, 하위 세션 보고 취합, 전체 시험(`pytest -n 8` 전체), 충돌 조정, 사용자 보고와 결정 요청.
- **하위 세션**(영역별): 자기 영역의 코드·시험·문서를 작은 조각으로 끝내서 착륙시킨다. 사용자에게 직접 묻지 않는다. 결정이 필요하면 최종 보고에 "결정 필요"로 적는다.
- 모든 보고와 사용자 대상 문구는 **한국어**.

## 절대 규칙
1. **운영 DB(`postgresql:///inazina`)에는 쓰지 않는다.** 읽기 전용 덤프(`pg_dump`)로 복사본을 만들어 쓴다. 운영에 반영해야 하는 데이터·스키마 작업은 명령과 검증 결과를 보고에 적고 사용자 승인을 기다린다.
2. 사용자의 서버(4310~4315)와 프로세스를 건드리지 않는다. 시험용 서버는 자기 포트 대역에서만 띄우고 **자기가 띄운 PID만** 종료한다(`pkill`, `killall` 금지).
3. 작업은 `~/Desktop/inazina`에서 직접 한다. 새 worktree·브랜치를 만들지 않는다. master에 직접 올린다.
4. 공유 작업 폴더에서 다른 세션의 미커밋 변경을 파괴하는 git 명령을 쓰지 않는다: `git stash`, `git checkout -- <파일>`, `git restore`, `git reset --hard`, `git clean`, `git rebase`, 강제 푸시 금지. `git add -A`와 `git commit -a`도 금지(남의 변경이 딸려 간다).
5. 변경은 반드시 **`tools/land_commit.py`로 착륙**한다(사용법은 그 파일 머리말). 잠금, 변경 조각 선택, 깨끗한 체크아웃에서 컴파일·시험을 한 뒤에만 master가 움직인다. 다른 세션이 같이 편집하는 공유 파일은 `--files`가 아니라 `--hunks 경로=키워드`로 자기 조각만 올린다. 키워드는 자기 변경에만 나오는 단어로 고른다.
6. 시험용 DB·포트·임시 파일은 에이전트 이름이 들어간 고유한 이름을 쓰고, 끝나면 지운다(`dropdb`).
7. 임시 스크립트로 `DatabaseManager` 등을 만들 때는 반드시 `INAZINA_PG_DSN`을 자기 복사본 DB로 지정한다. 지정하지 않으면 기본값인 운영 DB(`inazina`)에 접속해 스키마 동기화가 실행된다(2026-11-25 사고). pytest는 conftest가 격리해 주므로 안전하다.

## 공유 파일 (여러 영역이 만진다 → `--hunks`로만 착륙)
`web_api/server.py`, `apps/api/src/index.ts`, `apps/dashboard/src/App.tsx`, `apps/dashboard/src/nav.ts`, `database/currency_db.py`, `docs/controls_laplace.md`, `docs/roadmap_laplace.md`, `docs/architecture.md`, `tests/test_architecture.py`, `docs/team_protocol.md`

## 시험
- 자기 영역의 시험만 돌린다(`-n 4` 이하). **전체 시험은 통합관리 세션만** 돌린다(여러 세션이 동시에 전체를 돌리면 기계가 느려져 시험이 흔들린다).
- 모든 시험은 PostgreSQL에서 돈다(SQLite 지원은 종료됨, `docs/postgres-cutover-runbook.md`). 새 표·열은 코드의 스키마 정의(SQLite DDL)로 만들고 시작할 때 `pg_schema_sync`가 PostgreSQL에 반영한다.
- 금액은 Decimal·통화 최소단위. 새 금액 열은 금액 필드 등록부에 분류하거나(원장 금액) 사유와 함께 면제(정책값)한다. `tests/test_financial_close_control.py`가 이를 검사한다.
- `managers/`는 계층 규칙이 있다(`tests/test_architecture.py`, `docs/architecture.md`): 같은 계층이나 낮은 계층만 가져온다. 함수 안의 가져오기도 센다.
- 시험은 거짓 통과를 만들지 않는다. 주석 처리한 시험, 문자열로만 존재하는 시험은 증빙이 아니다.

## 완료 기준 (`docs/roadmap_laplace.md`의 "완료 판정")
코드와 마이그레이션, 사용자·관리자 웹 접근, 권한·감사, 원장 대사, 실패·재실행 경계, 해당 회귀, 통합 검증을 확인한 뒤에만 완료로 쓴다. 근사값·가정은 규제 산식처럼 표현하지 않는다. 과거를 재구성할 수 없으면 거래를 꾸미지 않고 baseline으로 남긴다.

## UI
승인된 목업(https://claude.ai/artifact/MZJfLZjEEHciSWjx8ET93r)의 범위 안에서 만든다. 목업에 없는 새 화면·큰 디자인 변경은 만들기 전에 보고한다. 관리자 쓰기 동작은 확인창과 감사 기록을 갖춘다. 표는 한 줄(가로 스크롤 허용).

## 보고 형식 (최종 메시지)
1. 한 일(착륙한 커밋 해시) 2. 증거(시험 수, 측정값, 화면 확인 여부) 3. 남은 일 4. 사용자 결정이 필요한 것 5. 다른 영역에 영향·주의. 짧게, 사실만. 확인하지 못한 것은 확인하지 못했다고 쓴다.
