# 통화 금액 필드 인벤토리 (M4 진행 중)

`financial_amount_snapshots`는 금액 원본 필드별로 통화 코드, 소수 자릿수, 정수 최소단위 값을 보존·검사한다. 생성 규칙은 [`currency_db.py`](../database/currency_db.py)의 `_init_currency_precision_triggers`에 명시되어 있다. 이 표는 현재 레지스트리 범위이며, 기존 REAL/INTEGER 업무 필드를 모두 권위 정수 필드로 바꿨다는 뜻은 아니다.

| 영역 | 포함 테이블/필드 | 통화 출처 |
|---|---|---|
| 고객 | `users.cash`, `users.annual_income`; `accounts.balance`, `principal`, `insurance_paid`, `uninsured_claim` | 거주국 통화 / 계좌 quote currency |
| 상품 | `saving.deposit_amount`; `liability.loan_limit`, `repayment_amount`; `investment.unit_price`, `face_value`, `dividend_per_share`; `treasury_bond_products.face_value`, `market_price`, `total_supply`, `sold_amount` | 상품 quote currency |
| 시장 | 현 시세(`market_assets.price_minor_units`, 회사주가 `company.share_price_minor_units`, 투자상품 `investment.unit_price_minor_units`) 및 `market_price_history.price_minor_units`(quote-currency 최소단위와 legacy 값 양방향 동기화); `dividend_per_share`; `market_orders.limit_price`, 주문예치금(`reserved_cash_minor_units` 권위 최소단위 열, `reserved_cash` 미러); 체결·최근 체결가(`market_trade_history.price_minor_units`, `market_last_trade.price_minor_units` 권위 최소단위 열, legacy `price` 미러); 실행 이벤트(`market_execution_events.price_minor_units`, `gross_minor_units`, `tax_minor_units`, `seller_proceeds_minor_units` 권위 최소단위 및 공통 market journal) | 종목/주문 기초자산의 quote currency |
| 대출·담보 | 원금·잔액(`principal_minor_units`, `outstanding_balance_minor_units` 권위 최소단위 열)·ECL 충당금(`ecl_allowance_minor_units`)·상각 후 미회수 메모잔액(`charged_off_balance_minor_units`)·담보평가액(`last_value_minor_units`)·담보 처분 비용/수익 최소단위 열·취급/필수비용·담보·회수 필드; 상환 스케줄 원금/이자/수수료/지급 필드; ECL 평가 스냅샷 EAD/손실 최소단위 열; 청산 가격/대금; 발생이자 잔여(`interest_remainder_subminor_units`, `overdue_interest_remainder_subminor_units`) | 계약/대출 quote currency |
| 채권·DR | 국채 발행상품(`face_value`, `market_price`, `total_supply`, `sold_amount`) 및 보유 lot(`principal`, `face_value`, `acquisition_cost`)의 `*_minor_units` 정수 열과 양방향 레거시 동기화; 국채 쿠폰·만기상환 불변 지급 이벤트 및 공통원장; DR movement 원가; DR deviation 표본·경보 가격/기준가 | 채권/DR 프로그램 통화 |
| 법인·기관·세금 | 기관 자본/이익잉여금/보험한도, 기관 P&L 원장 금액 및 기준통화 금액, 세금 총액/세액/순액(`tax_transactions`의 최소단위 열 및 gross=tax+net 불변식) | 기관/거래의 명시 통화 |
| 펀드 | 펀드 현금/NAV 및 펀드 현금원장 금액/잔액 | 펀드 상품 통화 |

가격·원금·현금·수수료·세금·충당금은 금액으로 분류한다. 이자율, 수익률, 비율, 수량/주식수, 확률, 만기/epoch, 변동성 및 지수 민감도는 통화 금액이 아니므로 이 레지스트리의 대상이 아니다. 가격×수량의 평가액은 주문/거래/원장 경로에서 별도로 검증되어야 한다.

## 아직 완료되지 않은 이관

- 애플리케이션 소비자는 아직 여러 곳에서 `REAL`/`float` 원본값과 계좌 micro-unit(`balance_units`)을 직접 사용한다. 세금 총액/세액/순액, 대출 원금/잔액, 충당금, 상각 메모잔액, 상환예정/납부 구성요소, 시장 주요 가격/체결 및 국채 발행/보유 금액은 통화 최소단위 필드와 호환 동기화·일부 불변식을 제공하지만, 일부 업무 소비자는 여전히 레거시 수치 열을 읽는다. 나머지 이자·수수료·외환환산·상품/NAV·채권 평가 및 소비자 경로는 계속 이관 대상이며 스냅샷만 등록된 금액은 권위 업무필드를 대체하지 않는다.
- 모든 필드의 원천 이벤트·중간 계산·DB 저장·웹/Discord 표시 소비자를 연결해 이 인벤토리와 대조하는 전사 데이터 계보 테스트가 남아 있다.
- 대출 정상·연체 이자 잔여는 통화 최소단위의 1/1,000,000 단위 정수로 보존하고, 기존 REAL 열은 호환용이다. 이는 발생 잔여의 정밀도 보완이며 이자 계산 전 과정이나 대출 원장 전체의 Decimal/정수 이관은 아니다.
- 정기 상환의 회차 예정액·미납액·잔액 차감은 이제 상환표/대출의 통화 최소단위 정수 열을 우선 사용한다. 기존 소비자 경계로 내보낼 때만 해당 통화 금액으로 변환한다. 레거시 상환과 이자 산출 등 대출 전체가 이관된 것은 아니다.
- 레거시 주기상환도 약정 상환액·대출 잔액을 통화 최소단위로 변환해 정수 연산하고, 자금 차감·기관 분개·잔액 변경에 동일 금액을 사용한다. 다른 대출 금액 경로와 회사/은행 전체 현금흐름 대사는 여전히 미완료다.
- 수동/중도상환은 대출 잔액·지급액·중도상환수수료·충당금 회수액·총 고객 차감을 계약통화 최소단위 정수로 제한해 잔액 변경 및 BankJournal 분개에 같은 금액을 사용한다. ECL/provisioned-loss의 모든 경로와 원장 전체 현금흐름 대사까지 완료됐다는 뜻은 아니다.
- 고객 지갑과 공통원장 대사는 이제 각 이벤트의 통화/정밀도, 기록시각, 매핑별 차변·대변 및 금액을 직접 비교한다. 은행·회사·시장 거래 전체의 전사 지급 연결은 별도 미완료다.
- 체결 이력과 최근 체결가는 종목 호가통화의 `price_minor_units`를 보유하며 기존 REAL `price`를 양방향 호환한다. 최근 체결/차트용 이력 API는 정수 최소단위에서 가격을 복원한다. 상장 자산·회사 주식·투자상품의 현 시세와 과거 가격 이력도 통화 최소단위 열과 legacy 가격값 양방향 동기화를 추가했으나, 일부 가격결정 소비자는 아직 legacy 가격값을 읽는다. 시장의 지정가 최소단위 권위화와 전 consumer migration, 체결 cash/share movement와 clearing 전사대사는 아직 미완료다.
- 미체결 매수 주문 예치금은 `reserved_cash_minor_units` 정수 권위값을 사용한다. 기존 주문은 quote currency 정밀도로 이관하며 부분체결 때 지정가×체결수량을 최소단위 정수로 차감하고, 취소·예약거절 환불도 이 값에서 복원한다. 신규 주문의 실제 현금 예치/환불 wallet ledger 항목을 `market_order_wallet_links`에 연결하며, 연결된 순차감액 = 체결 총액 + 잔여 예치액을 `market_order_wallet_cash` 대사로 검사한다. 과거 주문은 마이그레이션 시점 baseline으로 격리한다. 지정가 자체, 증권 보유 이동 및 clearing/전사대사는 아직 미완료다.
- `AccountingJournal.reconcile_subledgers`는 표준 자산 매수 주문의 활성 예약 잔액이 지정가 최소단위×잔량과 일치하는지 점검하고, 부분체결/종결 주문의 과다·누락 예약을 finding으로 노출한다. 이는 주문 예약 내부 대사이며 고객 현금, 체결대금, 증권 보유, 기관/회사 장부 간 결제 대사는 아니다.
- 신규 표준 현물 매칭은 양측 주문/당사자와 quote-currency 최소단위 가격·총액·세금·순대금을 불변 `market_execution_events`에 기록하고 공통 market journal에 균형 전기한다. 주문별 기존 체결량 baseline 이후의 증분 체결수량 및 이벤트별 공통 분개를 대사한다. 체결 처리 중 실제 생성된 고객 지갑 원장 entry를 `market_execution_wallet_links`에 불변 연결하고, 참조 무결성·통화/정밀도 및 매도자 순수령액을 체결 이벤트와 대사한다. 예약 시점 매수금 인출, 보유증권 이동, 발행자 결제, 옵션·선물·분할주 매처와 clearing의 전사 연결은 남아 있다.
- 국채 쿠폰·만기상환은 이제 통화 최소단위의 불변 지급 이벤트를 생성하고 같은 트랜잭션에서 `treasury_bond` 공통원장에 전기한다. 이벤트-분개 계정/금액/통화/기록시각 및 고아/누락 전기를 대사한다. 국채 매수 및 secondary sale/시가평가의 전체 현금·증권 결제 연결은 남아 있다.
- 통화·자산 유형별로 빠진 금액 필드가 없는지 스키마 전체 정밀 검토를 해야 한다. 특히 옵션/파생, 미결제·예약, 수탁 지급, 배당 청구, 법인/펀드 보조원장과 과거 가격 기록의 누락 검증이 필요하다.
- 공통 원장의 고객 지갑 전체 자동 전기, 전체 보조원장 대사, 과거 잔액 재구성, 부분 실패·재실행·기간 마감/정정 통합 검증도 미완료다.
