# 모듈 구조와 경계

Inazina는 프로세스 하나, SQLite DB 하나를 쓰는 모놀리스다. 통화·국고·주문·계좌가 한 트랜잭션에서
함께 바뀌어야 하므로 서비스를 나누지 않고, 대신 `managers/` 안의 의존 방향을 계층으로 고정한다.

## 계층

| 계층 | 역할 | 모듈 |
|---|---|---|
| L0 기반 | 다른 매니저에 의존하지 않는 계산·장 운영 | `_price_math`, `price_backend`, `stochastic_price_engine`, `auto_target_engine`, `movement_history_manager`, `order_validation`, `market_session_manager`, `trading_warning_manager` |
| L1 화폐·금리 | 지갑·환율·국고, 금리, 회계 원장, 개인 신용점수 | `currency_manager`, `rate_manager`, `financial_manager`, `bank_journal`, `accounting_journal`, `credit_score_manager` |
| L2 도메인 | 은행·이자·환전·채권·옵션·선물·ELS·펀드·투자·주문·기업행위·복권·사용자·시세·CB/BW·스왑·랩·IMA·법인 자본·준법(KYC)·신용위험·DR | `bank_manager`, `interest_manager`, `currency_exchange_manager`, `treasury_bond_manager`, `option_manager`, `futures_manager`, `els_manager`, `fund_manager`, `investment_manager`, `fractional_trading_manager`, `foreign_securities_manager`, `corporate_actions_manager`, `order_book_manager`, `lottery_manager`, `user_manager`, `nationality_manager`, `market_manager`, `capital_markets_manager`, `company_account_manager`, `compliance_manager`, `credit_risk_manager`, `depositary_receipt_manager`, `dr_deviation_monitor_manager`, `dr_dividend_tax_manager` |
| L3 관리·조립 | 관리자 명령, 상품 카탈로그 초기화 | `admin_manager`, `admin_control_manager`, `admin_commands_manager`, `catalog_manager` |

규칙:

- 모듈은 같은 계층이나 아래 계층만 import한다. 함수 안의 지연 import도 포함한다.
- `database/` 패키지는 `managers/`를 import하지 않는다.
- 새 매니저 파일은 위 표와 `tests/test_architecture.py`의 `LAYERS`에 계층을 정해 추가한다.

`tests/test_architecture.py`가 이 규칙을 검사한다.

## 알려진 부채

- **L2 내부 순환 의존**: `bank ↔ interest ↔ currency_exchange`, `order_book ↔ fund ↔ investment ↔ fractional_trading`,
  `corporate_actions ↔ foreign_securities` 등이 서로를 import한다. 순환을 끊을 때는 공통 부분을 L1로 내리거나,
  호출하는 쪽이 필요한 매니저를 인자로 넘기는 방식을 쓴다.
- **테이블 소유권**: 매니저가 다른 도메인의 테이블에 SQL을 직접 실행한다. 잔액은 `CurrencyManager.change`,
  국고는 `treasury_receive`/`treasury_pay`를 거치는 것처럼, 다른 도메인의 데이터는 그 도메인의 공개 함수로 바꾼다.

## 참조 무결성

SQLite 외래키 검사(`PRAGMA foreign_keys=ON`)는 모든 연결에서 켜져 있다. 선언된 REFERENCES는 12개뿐이고, 나머지 참조
(사용자 ID, 통화, 국적, 상품 코드, 계좌번호, 대출 ID)는 `database/integrity.py`가 규칙으로 검사한다.

```
python -m database.integrity        # 운영 DB를 읽기 전용으로 검사, 위반이 있으면 종료코드 1
```

- 취소·체결이 끝난 주문은 삭제·만기된 상품 코드를 가질 수 있는 이력이라 검사하지 않는다.
- `exchange_id`는 거래소 행이 필요할 때 만들어지고 별칭도 허용하므로 검사하지 않는다.
- 새 테이블에 다른 테이블을 가리키는 열을 추가하면 `database/integrity.py`의 규칙에도 추가한다.
