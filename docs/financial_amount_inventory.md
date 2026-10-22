# 통화 금액 필드 인벤토리 (M4 진행 중)

`financial_amount_snapshots`는 금액 원본 필드별로 통화 코드, 소수 자릿수, 정수 최소단위 값을 보존·검사한다. 생성 규칙은 [`currency_db.py`](../database/currency_db.py)의 `_init_currency_precision_triggers`에 명시되어 있다. 이 표는 현재 레지스트리 범위이며, 기존 REAL/INTEGER 업무 필드를 모두 권위 정수 필드로 바꿨다는 뜻은 아니다.

| 영역 | 포함 테이블/필드 | 통화 출처 |
|---|---|---|
| 고객 | `users.cash`, `users.annual_income`; `accounts.balance`, `principal`, `insurance_paid`, `uninsured_claim` | 거주국 통화 / 계좌 quote currency |
| 상품 | `saving.deposit_amount`; `liability.loan_limit`, `repayment_amount`; `investment.unit_price`, `face_value`, `dividend_per_share`; `treasury_bond_products.face_value`, `market_price`, `total_supply`, `sold_amount` | 상품 quote currency |
| 시장 | 현 시세(`market_assets.price_minor_units`, 회사주가 `company.share_price_minor_units`, 투자상품 `investment.unit_price_minor_units`) 및 `market_price_history.price_minor_units`(quote-currency 최소단위와 legacy 값 양방향 동기화); `dividend_per_share`; 지정가(`market_orders.limit_price_minor_units` 권위값 및 `limit_price_decimal_places` 통화 정밀도 snapshot, legacy `limit_price` 미러), 주문예치금(`reserved_cash_minor_units` 권위 최소단위 열, `reserved_cash` 미러); 체결·최근 체결가(`market_trade_history.price_minor_units`, `market_last_trade.price_minor_units` 권위 최소단위 열, legacy `price` 미러); 실행 이벤트(`market_execution_events.price_minor_units`, `gross_minor_units`, `tax_minor_units`, `seller_proceeds_minor_units` 권위 최소단위 및 공통 market journal) | 종목/주문 기초자산의 quote currency |
| 대출·담보 | 원금·잔액(`principal_minor_units`, `outstanding_balance_minor_units` 권위 최소단위 열)·ECL 충당금(`ecl_allowance_minor_units`)·상각 후 미회수 메모잔액(`charged_off_balance_minor_units`)·담보평가액(`last_value_minor_units`)·담보 처분 비용/수익 최소단위 열·취급/필수비용·담보·회수 필드; 상환 스케줄 원금/이자/수수료/지급 필드; ECL 평가 스냅샷 EAD/손실 최소단위 열; 청산 가격/대금; 발생이자 잔여(`interest_remainder_subminor_units`, `overdue_interest_remainder_subminor_units`) | 계약/대출 quote currency |
| 채권·DR | 국채 발행상품(`face_value`, `market_price`, `total_supply`, `sold_amount`) 및 보유 lot(`principal`, `face_value`, `acquisition_cost`)의 `*_minor_units` 정수 열과 양방향 레거시 동기화; 국채 쿠폰·만기상환 불변 지급 이벤트 및 공통원장; DR movement 원가; DR deviation 표본·경보 가격/기준가 | 채권/DR 프로그램 통화 |
| 법인·기관·세금 | 기관 자본/이익잉여금/보험한도, 기관 P&L 원장 금액 및 기준통화 금액, 세금 총액/세액/순액(`tax_transactions`의 최소단위 열 및 gross=tax+net 불변식) | 기관/거래의 명시 통화 |
| 펀드 | 펀드 현금/NAV 및 펀드 현금원장 금액/잔액 | 펀드 상품 통화 |

가격·원금·현금·수수료·세금·충당금은 금액으로 분류한다. 이자율, 수익률, 비율, 수량/주식수, 확률, 만기/epoch, 변동성 및 지수 민감도는 통화 금액이 아니므로 이 레지스트리의 대상이 아니다. 가격×수량의 평가액은 주문/거래/원장 경로에서 별도로 검증되어야 한다.

## 아직 완료되지 않은 이관

### 2026-10-20 스키마형 재점검 (초기화된 SQLite 스키마 기준)

임시 DB를 새로 초기화하고 모든 테이블의 `PRAGMA table_info`를 순회해 금액 이름 패턴(`amount/balance/price/cost/fee/tax/cash/principal/income/revenue/proceeds/premium/capital/allowance/payment/deposit/dividend/claim`)과 REAL/float·INTEGER 저장형을 대조했다. 이는 스키마 필드 후보를 찾는 정적 휴리스틱이며, 모든 코드 소비자·런타임 migration 이후 데이터·웹/Discord 표시 경로를 증명하는 검사는 아니다.

- canonical 최소단위 대응을 갖춘 호환 REAL 필드: `bank_deficit_alerts.cash`; `depositary_receipt_movements.underlying_average_cost/holder_underlying_average_cost/custody_underlying_average_cost`; `dr_deviation_samples`·`dr_deviation_alert_events`의 `dr_price/reference_value`; `aml_alerts.amount`; `financial_institutions.equity_capital`; `accounts.reference_price`; `collateral_liquidation_events.execution_price`; `deposit_insurance_claims.payout_amount`; `market_trade_history.price`; `market_last_trade.price`; `financial_institution_ledger.amount/base_amount`. DR 이동 원가는 원주·보유자·수탁 통화를 각각 분리해 최소단위/scale을 스냅샷하며, 수탁 평균원가 소비는 canonical 정수 필드를 읽는다. 이 대응 열이 있다는 사실만으로 모든 소비자와 결제원장이 이관된 것은 아니다.
- `tax_transactions.gross_amount/tax_amount/net_amount`는 통화별 최소단위 정수 대응을 갖는다. 입력/update 경계의 native 통화 산식 `gross = tax + net`을 DB guard가 강제한다. 신규 양수 세금 행은 현재 일반소득 원천징수·배당·예금이자 경로에서 append-only `tax_transaction_wallet_links`를 통해 실제 국고 지갑 입금에 연결하고, 대사는 누락/비국고/비양수 영수증과 같은 통화일 때 세액 차이를 검출한다. 기존 데이터는 startup watermark로 재구성 불가 baseline에 격리한다. 외화 환산 시 원천통화 세액과 도착통화 간 FX fixing/변환 계산을 링크 자체가 재현하지 않으며, 시장 체결·채권 등 별도 세금 경로와 모든 국고·기관 분개 간 통합대사는 여전히 남아 있다.
- REAL이지만 금액이 아닌 것으로 분류한 후보: 금리·수수료율·배당률·비율·민감도, 시간(epoch), 수량(`deposited_underlying_quantity`), 유동성/시장 파라미터, 신용모형 점수·정책값. 시장 지수·기준 가격(`market_exchange_index_history.index_value`, `market_simulation_state.latent_price`, `market_auto_targets.anchor_price/target_price`, `market_event_targets.*_price`, `fund_components.base_price`, `els_underlyings.initial_price/last_price`)은 통화 현금은 아니지만 가격표시 단위 및 quote currency를 명확히 할 필요가 있다.
- INTEGER로 저장되지만 스키마명만으로 전용 최소단위 권위 필드를 확인할 수 없었던 통화 금액 후보에는 고객 현금/연소득, 상품 예치/상환액, `user_assets.average_cost`, 옵션 strike/premium/collateral, 펀드 현금원장, 회사자본/배당, 대출 수수료·담보, 예금보험 한도/청구, 회사청산·DR 배당·ELS 지급, 복권 금액, 송금 수수료가 포함된다. 일부는 NWX whole-unit 전용, 별도 원장 또는 snapshot으로 관리되므로 이름만으로 이관 여부를 단정할 수 없다. 업무별 quote currency와 권위열·소비자를 인벤토리에 매핑해야 한다.

이 패스는 REAL/INTEGER 후보 명단을 추가하지만 업무원천→중간 산식→저장→API/화면 소비자 lineage까지 닫지 않으므로 M4 첫째 체크 항목은 미완료로 둔다.

### 시장 지정가 추적 (확인 범위: 표준 자산 주문, 미완료)

`/주문`·웹/Discord 주문 진입은 `OrderBookManager.place_limit_order`로 모이고, 표준 주문은 `market_orders.limit_price`에 기록된다. 현재 새 `limit_price_minor_units`와 정밀도 snapshot은 DB migration/호환 trigger 및 `AccountingJournal.reconcile_subledgers`에서 사용하지만, 체결 매칭(`_match_incoming_order`), 부분체결 환불·정산(`_settle_primary_fill`/`_settle_fill`), 일부 특수 매처는 계속 legacy `limit_price`를 읽는다. 웹 호가 API와 Discord 주문 표시도 legacy 정수 가격을 그대로 반환/표시한다. 따라서 이 경로의 출발점·저장·대사·표시 소비자는 찾아 기록했으나, 입력 parser/quote-currency 금액 표현, 모든 매처의 canonical 소비, 고객 잔액의 최소단위 처리까지 종단 이관한 것은 아니다. 옵션·선물·외화·분할주 경로는 각기 별도 검증이 필요하다.

- 애플리케이션 소비자는 아직 여러 곳에서 `REAL`/`float` 원본값과 계좌 micro-unit(`balance_units`)을 직접 사용한다. 세금 총액/세액/순액, 대출 원금/잔액, 충당금, 상각 메모잔액, 상환예정/납부 구성요소, 시장 주요 가격/체결 및 국채 발행/보유 금액은 통화 최소단위 필드와 호환 동기화·일부 불변식을 제공하지만, 일부 업무 소비자는 여전히 레거시 수치 열을 읽는다. 나머지 이자·수수료·외환환산·상품/NAV·채권 평가 및 소비자 경로는 계속 이관 대상이며 스냅샷만 등록된 금액은 권위 업무필드를 대체하지 않는다.
- 모든 필드의 원천 이벤트·중간 계산·DB 저장·웹/Discord 표시 소비자를 연결해 이 인벤토리와 대조하는 전사 데이터 계보 테스트가 남아 있다.
- 대출 정상·연체 이자 잔여는 통화 최소단위의 1/1,000,000 단위 정수로 보존하고, 기존 REAL 열은 호환용이다. 이는 발생 잔여의 정밀도 보완이며 이자 계산 전 과정이나 대출 원장 전체의 Decimal/정수 이관은 아니다.
- 정상 이자 발생, 연체이자 및 변동금리 재산정은 `outstanding_balance_minor_units` 기준으로 읽고 발생 이자를 최소단위 정수로 누적한다. 단리/복리 금액 산식과 ACT/360·ACT/365 분율은 Decimal이며 subminor 잔여도 정수로 보존한다. 30/360 날짜 경계 및 담보 재평가/회수 등 다른 소비자와 일부 계약 원금·잔액은 여전히 기존 float/REAL 경로여서 대출 계산 전체가 canonical화된 것은 아니다.
- 변동금리 재산정 이자는 기관 손익 원장, BankJournal 및 공통원장에 같은 참조키로 연결하고 최신 대출 커서를 재조회해 중복 발생을 방지한다. 대출 전체 생애주기 분개와 전사 대사가 완료된 것은 아니다.
- 정기 상환의 회차 예정액·미납액·잔액 차감은 이제 상환표/대출의 통화 최소단위 정수 열을 우선 사용한다. 기존 소비자 경계로 내보낼 때만 해당 통화 금액으로 변환한다. 레거시 상환과 이자 산출 등 대출 전체가 이관된 것은 아니다.
- 레거시 주기상환도 약정 상환액·대출 잔액을 통화 최소단위로 변환해 정수 연산하고, 자금 차감·기관 분개·잔액 변경에 동일 금액을 사용한다. 다른 대출 금액 경로와 회사/은행 전체 현금흐름 대사는 여전히 미완료다.
- 수동/중도상환은 대출 잔액·지급액·중도상환수수료·충당금 회수액·총 고객 차감을 계약통화 최소단위 정수로 제한해 잔액 변경 및 BankJournal 분개에 같은 금액을 사용한다. ECL/provisioned-loss의 모든 경로와 원장 전체 현금흐름 대사까지 완료됐다는 뜻은 아니다.
- 고객 지갑과 공통원장 대사는 이제 각 이벤트의 통화/정밀도, 기록시각, 매핑별 차변·대변 및 금액을 직접 비교한다. 은행·회사·시장 거래 전체의 전사 지급 연결은 별도 미완료다.
- 체결 이력과 최근 체결가는 종목 호가통화의 `price_minor_units`를 보유하며 기존 REAL `price`를 양방향 호환한다. 최근 체결/차트용 이력 API는 정수 최소단위에서 가격을 복원한다. 상장 자산·회사 주식·투자상품의 현 시세와 과거 가격 이력도 통화 최소단위 열과 legacy 가격값 양방향 동기화를 추가했다. 지정가(`limit_price_minor_units`)도 quote currency/정밀도 snapshot과 호환 mirror를 가지며 예약 reconciliation은 canonical 정수값을 사용한다. 다만 매칭 등 일부 가격결정 소비자는 여전히 legacy 지정가를 읽고, 전 consumer migration, 체결 cash/share movement와 clearing 전사대사는 아직 미완료다.
- 미체결 매수 주문 예치금은 `reserved_cash_minor_units` 정수 권위값을 사용한다. 기존 주문은 quote currency 정밀도로 이관하며 부분체결 때 지정가×체결수량을 최소단위 정수로 차감하고, 취소·예약거절 환불도 이 값에서 복원한다. 신규 주문의 실제 현금 예치/환불 wallet ledger 항목을 `market_order_wallet_links`에 연결하며, 연결된 순차감액 = 체결 총액 + 잔여 예치액을 `market_order_wallet_cash` 대사로 검사한다. 과거 주문은 마이그레이션 시점 baseline으로 격리한다. 지정가는 최소단위 필드로 저장되고 예약대사에 canonical 값이 쓰이지만, 체결 매칭 등 소비자 이관은 진행 중이며 증권 보유 이동 및 clearing/전사대사는 아직 미완료다.
- `AccountingJournal.reconcile_subledgers`는 표준 자산 매수 주문의 활성 예약 잔액이 지정가 최소단위×잔량과 일치하는지 점검하고, 부분체결/종결 주문의 과다·누락 예약을 finding으로 노출한다. 이는 주문 예약 내부 대사이며 고객 현금, 체결대금, 증권 보유, 기관/회사 장부 간 결제 대사는 아니다.
- 신규 표준 현물 매칭은 양측 주문/당사자와 quote-currency 최소단위 가격·총액·세금·순대금을 불변 `market_execution_events`에 기록하고 공통 market journal에 균형 전기한다. 주문별 기존 체결량 baseline 이후의 증분 체결수량 및 이벤트별 공통 분개를 대사한다. 체결 처리 중 실제 생성된 고객 지갑 원장 entry를 `market_execution_wallet_links`에 불변 연결하고, 참조 무결성·통화/정밀도 및 매도자 순수령액을 체결 이벤트와 대사한다. 예약 시점 매수금 인출, 보유증권 이동, 발행자 결제, 옵션·선물·분할주 매처와 clearing의 전사 연결은 남아 있다.
- 국채 쿠폰·만기상환은 통화 최소단위의 불변 지급 이벤트를 생성하고 같은 트랜잭션에서 `treasury_bond` 공통원장에 전기한다. 신규 이벤트는 실제 고객 지갑 입금 또는 펀드 현금 입금과 불변 링크로 연결되며 수취인·금액·통화/scale 차이를 대사한다. 과거 이벤트는 시작 watermark로 격리한다. NWX 국채 지급이 타국 고객의 국내통화 cash 계정에 같은 숫자로 기록되는 기존 경로가 있어 대사는 mismatch를 노출한다. 이 경우 FX 환산·clearing은 아직 미구현이다. 국채 매수 및 secondary sale/시가평가의 전체 현금·증권 결제 연결도 남아 있다.
- 예금보험 청구의 지급 통화 금액은 `payout_amount_minor_units`와 당시 `payout_decimal_places`를 권위값으로 저장한다. 신규 insert는 통화 scale·레거시 mirror와의 일치를 DB trigger로 강제하며, 기존 통화/금액이 유효한 행은 startup migration에서 backfill한다. 사용자 API는 정수 단위에서 표시값을 복원하고 기관 대사는 payout 보조원장의 합계 및 미확인/미러 불일치를 검사한다. 레거시 REAL 지급액은 읽기 호환 미러로 남고, 개별 지급을 고객 지갑/국고/환전 거래 ID와 직접 연결하는 전사 지급 대사 및 알 수 없는 과거 통화의 수동 정정 절차는 미완료다.
- 담보자산의 원가·최근가격·평가액·청산가격·청산비용/대금 및 청산 이벤트 기준가격·체결가격·총/순대금에 통화 최소단위 정수 필드를 연결했다. 레거시 청산행은 대출 계약통화로 backfill하고, mutable 담보 잔액은 insert/update 호환 trigger, 불변 청산 이벤트 가격은 insert 시 DB trigger 및 대사에서 legacy mirror와 검증한다. 담보 재평가는 가격·헤어컷 적용 평가액을 Decimal→최소단위 정수로 산출한다. 이는 담보 가격·평가 경로의 보강이며 담보 매각 전 과정의 고객/은행 현금·증권 원장 전사 결제 대사는 미완료다.
- IMA 만기정산의 기준가격도 계좌 계약통화 기준 `reference_price_minor_units`를 권위값으로 저장·backfill하고 레거시 금액은 최소단위 mirror로 정규화한다. 만기 수익률은 현재 기준가격과 최초 기준가격을 모두 통화 최소단위 정수에서 계산한다. NWX 기준 IMA 만기/은행손익 테스트가 통과했다. 다만 환산·현금 지급 분개 전체 경로는 별도 미완료다.
- 금융기관의 자기자본(`equity_capital`)도 기관 국적의 통화 및 정밀도 스냅샷과 `equity_capital_minor_units`를 저장하고, 기존 레거시 값 backfill/양방향 미러를 제공한다. 기관 자본 컴포넌트와 국가별 자본집계는 정수단위 원본에서 읽는다. 기관 자본 변경, 상품 심사 영향 및 기존 기관 ledger migration 대상 테스트가 통과했다. 관할 통화 자체가 변경될 때의 재표시/환산 회계와 여러 기관 통화 간 연결 대사는 남아 있다.
- AML 모니터링 alert의 금액은 통화별 최소단위 `amount_minor_units`와 정밀도 snapshot을 보유하며 기존 alert backfill·insert/update 호환·canonical display를 적용했다. sender/recipient가 각자의 국내통화로 금액을 산출하는 테스트와 compliance overview API 테스트가 통과했다. 원거래 FX fixing 자체를 경보에 스냅샷하고 지급원장까지 연결하는 AML 경보 전사 대사는 별도 미완료다.
- DR deviation sample/alert의 거래가격 및 환산 reference price도 DR 상장통화 최소단위 정수 필드에 고정하고 과거 표본 backfill 및 history API 노출을 추가했다. DR 원천징수 이벤트도 지급 통화 `decimal_places`, `gross_minor_units`, `tax_minor_units`, `net_minor_units`로 이관하고, USD 소수 통화의 과거 이벤트 backfill 및 `gross=tax+net`·지급 snapshot 대사를 추가했다. DR 프리미엄/할인 전이·버킷 중복·보존기한 및 DR 세금 테스트에서 금액 snapshot을 확인한다. underlying 통화→DR 통화 환산을 구성한 세부 fixing/시장가격 원천까지 재현하는 event snapshot은 별도 미완료다.
- 통화·자산 유형별로 빠진 금액 필드가 없는지 스키마 전체 정밀 검토를 해야 한다. 특히 옵션/파생, 미결제·예약, 수탁 지급, 배당 청구, 법인/펀드 보조원장과 과거 가격 기록의 누락 검증이 필요하다.
- 고객 지갑은 신규 현금/계좌 잔액을 공통 journal에 자동 전기한다. 원천징수 세금은 신규 국고 지갑 입금과 불변 연결하고, 과거 세금행은 watermark baseline으로 분리한다. 배당 지급은 고객 wallet 또는 펀드 현금 ledger의 실제 양수 행 하나에 불변 링크하며, 신규 레코드 대사에서 수취자·통화·scale·최소단위 금액/펀드 event를 검사한다. 펀드 NWX 현금 ledger도 공통 journal에 균형 전기·재실행 backfill·잔액 대사를 연결했다. 하지만 이 연결은 전사 자동 전기의 완료를 뜻하지 않는다. 상품 NAV/보유자산, 지급재원·회사손익, 세금 원천별 FX clearing 및 전체 보조원장 대사, 전사 과거 잔액 재구성, 부분 실패·재실행·기간 마감/정정 통합 검증은 여전히 미완료다.
- `AccountingJournal.trial_balance`는 원장/주체/통화 및 선택적 `[시작, 종료)` epoch 범위로 공통원장 계정별 차변·대변·순잔액을 상품통화 최소단위로 합산하고, 통화 scale snapshot 불일치를 거부한다. 이는 조회 도구이며 미연결 보조원장이나 빈/불균형 분개를 스스로 보정하지 않는다. 전체 회계범위 대사와 별도 사용해야 한다.
- 신규 표준 현물 체결은 `market_execution_holding_movements`에 실제 수량 이동을 거래별 정수로 보존한다. 일반 자산은 `user_assets.quantity`, 국채는 활성 `treasury_bond_holdings`의 액면 단위 수량과 비교하며, 1차 발행은 매수자 증가, 2차 매매는 매수자 증가·매도자 감소를 검증한다. 기존 체결은 별도 baseline으로 격리한다. 증권 이동 원장의 커버리지는 비분할 표준 현물에 한정되고 발행자 결제/clearing과 기타 매처의 전사 연결은 미완료다.
- 체결량 대사 baseline은 주문 현재 체결량과 이관 시점 이벤트 누계를 함께 1회 저장한다. 재기동 때 신규 주문이나 체결을 뒤늦게 baseline으로 넘기지 않으며, 주문 체결량 변화와 이벤트 누계 변화량을 비교한다.
