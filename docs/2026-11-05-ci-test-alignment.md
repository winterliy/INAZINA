# 2026-11-05 CI 실패 정리: 테스트를 앱 동작에 맞춘 항목

CI 실행 41669894256(main, 9382ceb)에서 63개 테스트가 실패했다.
이 문서는 **앱 코드는 고치지 않고 테스트만 현재 동작에 맞춘** 항목 중,
**의도한 변경인지 확인되지 않은 것**을 기록한다. 의도하지 않은 변경이라면
테스트가 아니라 앱 코드를 고쳐야 한다.

## 확인이 필요한 동작 변경 (미확인)

| 항목 | 이전 동작 | 현재 동작 | 맞춘 테스트 |
| --- | --- | --- | --- |
| 대출 실수령액 | 대출 전용 계좌(`loan_payout`)로 입금 | 사용자 현금 잔액으로 입금 ("입금처: 현금 잔액") | `tests/test_economy.py` (대출 2건) |
| 주식 1차 발행 대금 | 국고(treasury)에 적립 | 해당 법인의 `issuer_cash_accounts`에 적립 | `tests/test_economy.py` (1차 발행 1건) |
| 배당 재원 | 국고 또는 발행법인 | 항상 발행법인 현금 (`dividend_funding`이 `issuer`로 강제). 법인에 자금이 없으면 "법인의 NWX 배당 재원이 부족합니다" | `tests/test_shareholder_register.py`, `tests/test_company_accounts.py`, `tests/test_admin_expansion.py` |
| 우선주 생성 명령 | `우선주` 타입 전용 위치 인자 (`PR preferred 이름 모회사 …`) | 주식 생성으로 통합, `share_class`·`parent_company_code`를 JSON으로 지정 | `tests/test_admin_expansion.py`, `tests/test_fractional_trading.py`, `tests/test_structured_integration.py` |

## 앱 코드를 고친 항목

- `managers/market_session_manager.py` `_resume`: 전체 휴장 후 재개할 때 시장 요인 상태(`market_factor_state`)를
  휴장 시간만큼 미루는 조건이 "열린 거래소가 하나도 없음"이었다. 항상 열려 있는 시스템 거래소 `CURRENCY`가
  있어서 이 조건이 절대 참이 되지 않았다. `CURRENCY`를 제외하도록 한 줄 고쳤다
  (`tests/test_market_sessions.py::test_automatic_targets_and_model_memory_resume_without_downtime_replay`).
  3DSG(장외시장)도 항상 열려 있다면 같은 문제가 생기므로 의도를 확인해야 한다.

## 의도 확인이 필요한 추가 항목

- 국가 계정(`country`)은 `admin_users` 사용자 목록에서 제외된다(커밋 4b31850, 조직 화면으로 이동한 것으로 보임).
  `tests/test_web_api.py`, `tests/test_country_accounts.py`를 이에 맞췄다.
- 연결재무제표(`tests/test_company_accounts.py::test_consolidated_statements_eliminate_the_investment`):
  자회사 원장은 처음 조회할 때 자본금이 채워진다. 조회 전에 연결하면 자회사 자본이 20000으로 잡히고,
  조회 후에는 60000이라 영업권이 달라진다(58200 대 34200). 앱 로직이 아니라 조회 순서 의존으로 판단해
  테스트에서 `report('SUB')`를 먼저 호출하도록 했다. 연결 전에 자회사 원장을 자동으로 채우는 것이 맞는지 확인이 필요하다.
- 우선주 생성이 주식 생성으로 통합되어 `tests/test_admin_expansion.py`에서 배당 합계가 달라졌다
  (국고가 아닌 법인 현금으로 지급되는 주식 배당 2건).
- `tests/test_currency_home.py`, `tests/test_fund_accounting.py`: 1차 발행 대금이 국고가 아닌
  법인 현금 계좌로 들어가는 것에 맞췄다.

## 환경 변경에 따른 테스트 수정 (동작 변경 아님)

- 거래소·장외시장은 법인 국적과 같은 국가의 **등록된** 거래소만 선택할 수 있다
  (커밋 8435e36). 공용 `setUp`(`tests/test_inazina.py`)에 `EX`, `INZ`, `TEST`를 등록했다.
- `company` 테이블에 `trading_status` 컬럼이 생겼다. 직접 만든 테스트 테이블에 추가했다
  (`tests/test_price_gap_filler.py`).
- `search_indicators`가 `saving`/`liability` 테이블을 읽고 `products`를 반환한다
  (`tests/test_search_indicators.py`).
