# 통화 금액 필드 인벤토리 (M4 진행 중)

`financial_amount_snapshots`는 금액 원본 필드별로 통화 코드, 소수 자릿수, 정수 최소단위 값을 보존·검사한다. 생성 규칙은 [`currency_db.py`](../database/currency_db.py)의 `_init_currency_precision_triggers`에 명시되어 있다. 이 표는 현재 레지스트리 범위이며, 기존 REAL/INTEGER 업무 필드를 모두 권위 정수 필드로 바꿨다는 뜻은 아니다.

| 영역 | 포함 테이블/필드 | 통화 출처 |
|---|---|---|
| 고객 | `users.cash`, `users.annual_income`; `accounts.balance`, `principal`, `insurance_paid`, `uninsured_claim` | 거주국 통화 / 계좌 quote currency |
| 상품 | `saving.deposit_amount`; `liability.loan_limit`, `repayment_amount`; `investment.unit_price`, `face_value`, `dividend_per_share`; `treasury_bond_products.face_value`, `market_price`, `total_supply`, `sold_amount` | 상품 quote currency |
| 시장 | `market_assets.price`, `dividend_per_share`; `market_orders.limit_price`, `reserved_cash`; `market_trade_history.price`; `market_last_trade.price`; `market_price_history.price` | 종목/주문 기초자산의 quote currency |
| 대출·담보 | 원금·잔액(`principal_minor_units`, `outstanding_balance_minor_units` 권위 최소단위 열)·ECL 충당금(`ecl_allowance_minor_units`)·상각 후 미회수 메모잔액(`charged_off_balance_minor_units`)·담보평가액(`last_value_minor_units`)·담보 처분 비용/수익 최소단위 열·취급/필수비용·담보·회수 필드; 상환 스케줄 원금/이자/수수료/지급 필드; ECL 평가 스냅샷 EAD/손실 최소단위 열; 청산 가격/대금; 발생이자 잔여(`interest_remainder_subminor_units`, `overdue_interest_remainder_subminor_units`) | 계약/대출 quote currency |
| 채권·DR | `treasury_bond_holdings` 원금/액면/취득원가; DR movement 원가; DR deviation 표본·경보 가격/기준가 | 채권/DR 프로그램 통화 |
| 법인·기관·세금 | 기관 자본/이익잉여금/보험한도, 기관 P&L 원장 금액 및 기준통화 금액, 세금 총액/세액/순액 | 기관/거래의 명시 통화 |
| 펀드 | 펀드 현금/NAV 및 펀드 현금원장 금액/잔액 | 펀드 상품 통화 |

가격·원금·현금·수수료·세금·충당금은 금액으로 분류한다. 이자율, 수익률, 비율, 수량/주식수, 확률, 만기/epoch, 변동성 및 지수 민감도는 통화 금액이 아니므로 이 레지스트리의 대상이 아니다. 가격×수량의 평가액은 주문/거래/원장 경로에서 별도로 검증되어야 한다.

## 아직 완료되지 않은 이관

- 애플리케이션 소비자는 아직 여러 곳에서 `REAL`/`float` 원본값과 계좌 micro-unit(`balance_units`)을 직접 사용한다. 대출 원금/잔액, 충당금, 상각 메모잔액, 상환예정/납부 구성요소는 이제 통화 최소단위 열을 제공하지만 대출의 다른 금액, 이자 발생, 시장·상품·채권 값은 여전히 전체 권위 이관 대상이다. 나머지 스냅샷은 검증 가능한 정수 표현이지 업무 필드를 대체하지 않는다.
- 모든 필드의 원천 이벤트·중간 계산·DB 저장·웹/Discord 표시 소비자를 연결해 이 인벤토리와 대조하는 전사 데이터 계보 테스트가 남아 있다.
- 대출 정상·연체 이자 잔여는 통화 최소단위의 1/1,000,000 단위 정수로 보존하고, 기존 REAL 열은 호환용이다. 이는 발생 잔여의 정밀도 보완이며 이자 계산 전 과정이나 대출 원장 전체의 Decimal/정수 이관은 아니다.
- 통화·자산 유형별로 빠진 금액 필드가 없는지 스키마 전체 정밀 검토를 해야 한다. 특히 옵션/파생, 미결제·예약, 수탁 지급, 배당 청구, 법인/펀드 보조원장과 과거 가격 기록의 누락 검증이 필요하다.
- 공통 원장의 고객 지갑 전체 자동 전기, 전체 보조원장 대사, 과거 잔액 재구성, 부분 실패·재실행·기간 마감/정정 통합 검증도 미완료다.
