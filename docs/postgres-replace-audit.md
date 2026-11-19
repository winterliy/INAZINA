# PostgreSQL 전환 전 REPLACE 점검

## 처리 결과 (2026-11-18 이후)

- 금리 이력은 명시적인 충돌 갱신을 사용해 두 DB에서 같은 감사 이벤트를 남긴다.
- ELS 이전은 기존 상품 코드가 겹치면 이전을 중단하고 기존 상품·청약·이전 원본을 보존한다. 중복은 운영자가 확인해야 한다.
- 저축·대출 등록은 다른 종류의 같은 코드를 거부하고 거래 제한 등 생략된 설정을 보존한다. 투자 상품과 자산도 명시적으로 갱신한다.
- 회생·파산 정책은 기본키 충돌 시 명시적으로 갱신해 삭제·재삽입을 피한다.

아래는 수정 전 점검 기록이다. 임의의 `INSERT OR REPLACE`에 대한 변환기의 모든 SQLite·PostgreSQL 차이가 사라진 것은 아니다.

점검 기준: `INSERT OR REPLACE`와 `REPLACE INTO` 소스 검색, DB 사본의 고유 인덱스·외래 키·트리거, PostgreSQL SQL 변환기 및 생성 트리거. SQLite는 기본 설정에서 REPLACE의 암묵적 삭제에 DELETE 트리거를 실행하지 않는다. `recursive_triggers` 설정을 바꾸면 동작이 달라진다. PostgreSQL 변환은 기본키 충돌에서 `ON CONFLICT DO UPDATE`를 사용하므로 UPDATE 트리거가 실행되고, 기본키 외의 고유 제약 충돌을 REPLACE로 처리하지 않는다.

## 수정 전 확인한 위험

1. `database/currency_db.py`의 국가 금리 이력 트리거 4곳, 시장 금리 곡선 이력 트리거 2곳과 `managers/rate_manager.py`의 시장 금리 이력 기록 2곳은 복합 기본키를 사용한다. 같은 키를 덮을 때 SQLite는 삽입 이벤트를 남기지만 PostgreSQL은 UPDATE 이벤트를 남긴다. 시장 금리 이력은 BEFORE INSERT의 `replace` 기록도 있어서 PostgreSQL에서 `replace`와 `update`가 함께 남을 수 있다. 이력 이벤트의 의미와 원장 대조를 양쪽에서 동일하게 정의하고 충돌 재현 테스트를 추가한 뒤 전환해야 한다.
2. `database/els_db.py`의 이전 ELS 상품 이전은 `els_products` 기본키 충돌 시 PostgreSQL에서 UPDATE를 실행한다. 이미 청약이 있으면 발행조건 UPDATE 불변 트리거가 이를 거부할 수 있다. 이전 DB 사본에서 상품 중복과 청약 존재를 점검하고, 불변 계약을 덮지 않는 이전 정책과 양쪽 DB 검증을 마련해야 한다.
3. `database/product_db.py`의 `register_saving`, `register_liability`, `register_investment`와 `database/asset_db.py`의 `register_asset`은 기존 행을 교체하는 저수준 헬퍼다. 현재 저장소에서 호출은 테스트에만 보이지만 외부 호출은 확인할 수 없다. PostgreSQL에서는 UPDATE 검증·불변 트리거가 실행되고 INSERT 트리거는 충돌 결과에 대해 실행되지 않는다. `investment`에는 ELS 청약 후 UPDATE 불변 규칙과 `fund_accounts`의 외래 키가 있다. SQLite의 삭제·재삽입 시 외래 키 동작도 UPDATE와 같지 않다. 특히 저축과 대출은 SQLite에서 별도 테이블이지만 PostgreSQL `bank_products`의 단일 `product_code` 기본키를 공유하므로 서로 다른 종류의 같은 코드는 원래 행을 덮을 위험이 있다. 전환 전 코드 중복을 차단하고, 이 헬퍼를 운영에서 사용하는지 확인해 명시적인 생성·수정 동작으로 대체해야 한다. 저축 REPLACE는 생략된 `transaction_policy`도 기본값으로 되돌린다.
4. 미커밋인 다른 세션의 `managers/insolvency_manager.py`에는 `insolvency_policies` REPLACE가 있다. 현재 정의의 유일한 키는 `nationality` 기본키이고 UPDATE/DELETE 트리거는 보이지 않는다. 해당 변경이 확정될 때 스키마와 감사 동작을 다시 확인해야 한다. 이 점검은 그 파일을 수정하지 않았다.

## 테스트 전용 사용처

`tests/test_financial.py`의 `accounting_period_closings`, `accounting_entries`, `accounting_lines`, `tests/test_fx_interest.py`의 `tax_bank_remittance_links`, `tax_bank_remittance_quotes`, `tests/test_currency_account_storage.py`의 두 activation 테이블은 REPLACE로 원장을 변조하려는 시험이다. 이들은 UPDATE/DELETE 불변 트리거 외에도 충돌을 검사하는 BEFORE INSERT 가드를 갖는다. `accounting_period_closings`와 `accounting_entries`에는 기본키 외 복합 UNIQUE가 있고 `tax_bank_remittance_links`에는 별도 UNIQUE 두 개가 있으므로, 기본키와 비기본키 충돌을 각각 양 DB에서 계속 시험해야 한다. UPDATE 트리거만으로 SQLite의 암묵적 삭제를 막는다고 가정하면 안 된다.

`tests/test_financial.py`의 `financial_share_history`, `tests/test_realistic_rate_system.py`의 `market_exchanges`, `tests/test_economy.py`의 `guild_countries`, `tests/test_movement_daily.py`의 `market_exchange_index_components`와 `leveraged_etf_state`는 테스트 데이터 설정용이며 확인한 유일 키는 기본키다. `market_exchange_indices`는 실제 테이블이 아니라 INSTEAD OF INSERT 트리거가 있는 호환 뷰다. `market_exchange_index_components`에는 스냅샷 정리용 DELETE 트리거가 있으므로, REPLACE가 그 삭제 트리거를 실행한다고 기대해서는 안 된다. PostgreSQL 변환에서는 INSERT 대신 UPDATE 트리거가 실행될 수 있다.

`database/pg_backend.py`와 `tools/build_postgres_triggers.py`는 위 SQL을 변환하는 구현·주석으로, 별도 업무 데이터 쓰기 사용처가 아니다. 이 문서는 위험을 기록한 것이며 변환기를 수정하거나 PostgreSQL 전환 안전성을 인증한 것은 아니다.
