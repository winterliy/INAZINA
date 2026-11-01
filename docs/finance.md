# 통화·재무·신용

국가별 통화, 회사 재무제표, 금액 필드 인벤토리, 개인 신용점수를 모았다. 앞으로의 현실화 계획은 [roadmap_laplace.md](roadmap_laplace.md)에 있다.

- [국적과 화폐](#국적과-화폐)
- [회사 재무제표](#회사-재무제표)
- [통화 금액 필드 인벤토리 (M4 종료 기준)](#통화-금액-필드-인벤토리-m4-종료-기준)
- [개인 신용점수](#개인-신용점수)

## 국적과 화폐

등록 서버 ID는 `guild_countries.guild_id`, 국적은 `guild_countries.nationality`로 설정한다.
국가 ID(사용자의 `nationality`)와 국가별 기본 통화 연결은 `country_currencies`에 저장하고, 통화 기준정보는 `currencies`에 둔다. 통화 목록에서 해당 통화를 사용하는 국가는 `country_currencies`를 통화 코드로 묶어 표시한다. 이 관계를 `currencies`에 중복 문자열로 저장하지 않아 국가나 통화 설정 변경 시 목록이 어긋나지 않는다. 모든 통화 참조에는 `currencies.code`를 사용한다.

`currencies`는 통화 코드·표시명·상태·기준 환율·소수 자릿수와 선택적 시장 자산(`asset_code`)을 정의한다. `market_assets`는 가격·거래·주문·차트 이력을 갖는 시장 상품이다. 예를 들어 `AAA`는 통화 기준정보이고 `CUR_AAA`는 `AAA`/기준 통화 시장 시세 상품이다. 시장 상품이 없는 통화는 `reference_rate`를 사용한다.

초기 기준통화와 통화 등록 목록은 [`config/initial_currency_catalog.json`](../config/initial_currency_catalog.json)에서 편집한다. `baseCurrency.code`, `baseCurrency.name`, `baseCurrency.nationality`가 새 설치의 기준통화·기본 국가이며, 선택한 기준통화는 `decimal_places: 0`이어야 한다. `currencies` 항목의 `code`, `name`, `nationality`, `reference_rate`, `decimal_places`가 초기 통화 등록값이고, `foreign_product_anchor`를 지정한 통화만 외화 주식·현물 상품 카탈로그를 생성한다. 이 설정은 신규 DB에서만 초기값으로 적용된다. 이미 초기화된 DB에서는 기준통화가 `currency_system_settings`에 저장되며, 설정 파일을 바꿔도 기존 통화·국가·상품을 덮어쓰지 않는다. 기존 DB는 웹 관리자 화면에서 개별 수정하거나 안전한 별도 마이그레이션을 해야 한다.
서버 관리자는 `/설치 [국명] [화폐]`로 신청한다. INAZINA 등록 여부와 관계없이 서버 소유자 또는 Discord 관리자 권한을 가진 사람만 신청할 수 있다.
신청 시 INAZINA 관리자(DB role=admin 및 초기 관리자)에게 발신 서버 ID·국명·화폐·신청자와 승인 명령어를 DM으로 전달한다. 신청만으로 국적 설정이 적용되지는 않는다. DM 전달 실패 시 신청자에게 실패를 알린다.
INAZINA 관리자 승인: 해당 서버에서 `$서버설치 낙원 <기준통화코드>` / `$서버설치 A국 AAA`. 신청 DM을 받은 관리자는 서버 ID를 포함해 `$서버설치 123456789 낙원 <기준통화코드>`로 설정한다.
등록하지 않은 사용자는 해당 서버에서 `/등록`한다. 국적과 `users.registration_guild_id`를 처음 등록할 때 저장하며 다른 서버를 이용해도 바뀌지 않는다.
DM에서 신규 시민을 등록하려면 등록 서버 정보가 없으므로 서버에서 실행해야 한다. 기존 시민의 조회·거래는 DM에서도 가능하다.
국가 화폐 연결이 없는 기존 시민은 `currency_system_settings.base_currency_code`를 사용한다.

### 현금·외환

국적 화폐가 기본화폐이며 그 외 모든 화폐는 외환이다. A국 시민에게 기준 통화도 외환이다.
`users.cash`는 사용자의 국적 화폐만 저장한다. 기준 통화 고정 지갑이 아니다.
외화는 별도 지갑 테이블에 저장하지 않고 `accounts`의 `account_type='currency_saving'`, `quote_currency`, `balance_units`에 저장한다. `balance_units`의 100만분의 1 정수는 환전·송금을 위한 내부 전송 단위일 뿐 회계 정밀도가 아니다. 실제 입력·정산 가능 자릿수는 해당 통화의 `decimal_places`를 따른다.
사용자는 국적 화폐와 같은 외환통장을 개설할 수 없다. 외화를 받거나 환전·거래하려면 그 화폐용 외환통장을 먼저 개설해야 한다.
기존 `currency_wallets`는 시작 시 한 번 이관한다. 국적 화폐 잔액은 `users.cash`로, 기존 기준 통화 현금과 그 밖의 외화는 통화별 `accounts` 외환통장으로 옮긴 뒤 테이블을 삭제한다.
`/내지갑`의 메인 요약은 본인 기본화폐로 평가하고, 통화별 현금에서 각 화폐의 실제 잔액을 표시한다.
지갑의 기준 통화 평가액·상세 금액도 본인의 기본화폐로 환산해 표시한다. 외환 잔액과 시장 호가·차트·환전·송금 양쪽 금액은 실제 대상 통화를 유지한다. 기준 통화라는 글자만 바꾸어 잘못된 액수를 표시하지 않는다.
`/송금`은 기본 출금 시 본인 기본화폐 지갑, 사용자 ID 수신 시 수신자의 기본화폐 지갑으로 송금한다. 본인 외환통장을 수신자로 선택하면 금액은 입금될 외화 기준이며, 필요한 출금 기본화폐와 수수료를 역산해 한 트랜잭션으로 환전한다.
출금계좌 자동완성에는 국적 화폐 지갑과 개설된 외환통장을 표시한다. 일반 통장 계좌번호로 직접 입출금할 수도 있다.
`/환전`은 사용하지 않는다. 환전하려는 통화의 본인 외환통장을 먼저 개설한 뒤 `/송금`의 수신자로 그 통장을 선택한다.
외환통장 잔액은 `balance_units`에 1통화의 100만분의 1 단위 정수로 저장하고, 통화별 `decimal_places`(0~6)로 실제 입력 가능 자릿수를 제한한다. 국적 화폐 지갑과 일반통장은 기존 `cash`·`balance` 네이티브 단위를 유지하므로 환전 송금은 소수 금액을 보존하지만, 일부 기존 투자·자산평가 경로는 여전히 정수 반올림을 사용한다.
본국 화폐의 주식·현물은 본국 현금 또는 본국 화폐 일반 통장으로 거래한다. 외화 상품은 해당 화폐 외환통장으로 거래하며 주문 취소 시 원래 출금 계좌에 예치금을 돌려준다.

### 기본소득

`$기본소득 [국가] [금액]`은 그 국가의 활성 시민에게만 1인당 [금액]을 그 국가의 화폐로 지급한다.
재원은 해당 국가 국고에서 같은 화폐로 차감하며 적자를 허용한다. 환전은 일어나지 않는다.
예: A국 화폐가 AAA이면 `$기본소득 A국 30000`은 A국 시민에게 30000 AAA씩 지급한다.
금액이 그 화폐의 최소 단위를 벗어나거나 등록되지 않은 국가면 전체 지급을 취소한다.
`basic_income_payments`는 지급 묶음 ID, 화폐, 지급액, 당시 환율, 지급시각을 보존한다.

### 상품 통화와 환율

`company`, `investment`, `market_assets`, `saving`, `liability`, `treasury_bond_products`, `option_contracts`의 `quote_currency`가 가격·계약금액의 통화 코드다.
`currency_products.currency_code`는 외환 예금 통화다. 계좌·대출·주문에도 통화 코드가 따로 저장된다.
기존 상품·계약은 기준 통화를 유지하며, 발행 후 통화를 바꾸어 기존 계약을 재표시하지 않는다.
상품생성의 선택 JSON에 `{"quote_currency":"ABC"}`를 쓸 수 있다. 주식·현물뿐 아니라 일반 예금·대출도 해당 통화를 국적 통화로 쓰는 사용자를 대상으로 개설할 수 있다. 계약 체결 뒤에는 상품 통화를 바꾸지 않는다.
`currencies.reference_rate`는 시세 상품이 없는 통화의 양수 기준값(문자열, 정확한 분수도 허용)이다. 시세 상품이 있으면 가격을 기준값으로 사용한다.
`currencies.rate_multiplier`는 그 기준값에 곱하는 양수 비율이다. 통화 간 환율은 두 기준값의 비율이며 소수 환율도 유지한다.
환율에는 `currency_liquidity_state`의 유동성 보정도 적용된다. 기준 공급량보다 발행·예치 총량이 늘면 해당 통화 가치는 완만하게 낮아지고, 국고 밖 유통 비율이 기준보다 줄면 추가로 낮아진다. 총량과 유통비율이 기준점과 같으면 보정값은 1이다.
총량은 그 화폐를 국적 통화로 쓰는 사용자의 `users.cash`·일반계좌 잔액과 해당 화폐 외환통장 잔액의 합계다. 국고 보유분은 총량에는 포함되지만 유통량에서는 제외한다. 신규 통화는 최초 발행 시의 총량·유통비율을 기준점으로 자동 기록한다.
DB Browser에서 `currency_liquidity_state`의 `issuance_elasticity`, `circulation_elasticity`, `min_multiplier`, `max_multiplier`를 조정할 수 있다. 기본값은 발행량 민감도 0.50, 유통 민감도 0.20, 보정 범위 0.25~4.00배다.
`$환율수정 A B 3`은 A 기준값을 유지하고 B 값을 조정해 1 A = 3 B를 만든다. 시세 상품이 있는 B는 가격 배수를 설정하므로 이후 자동 가격변동·사건도 계속 반영된다.

DB 위치: `/Users/sinjaehyeon/Documents/naconomics/database/inazina.db`.

### 화폐별 자릿수

`currencies.decimal_places`에 0~6을 저장한다. 기준 통화·KRW·JPY는 초기 0, USD·EUR·GBP 등은 초기 2, 다른 통화는 6이다.
`$상품수정 AAA currency 알파 CUR_AAA A국 '{"decimal_places":2}'`처럼 선택 JSON으로 바꿀 수 있으며 DB Browser로도 조절 가능하다.
통화별 금액 입력은 해당 자릿수까지만 허용하며 환산 지급은 수령 화폐의 최소 단위로 절삭한다. 표시에는 지정 자릿수를 사용한다(예: USD 12.50, KRW 12500).
`users.cash`, 예금·대출 원금과 이자, 수수료, 상환표, 담보평가액, 주문 예치금과 평균단가는 기준 통화 정수로 통일하지 않고 각 행의 국적·상품·계약 `quote_currency` 정밀도로 half-up 반올림한다. 따라서 기준 통화·JPY는 0자리, USD는 2자리다. SQL Browser 직접 입력에도 같은 트리거가 적용된다.
금융기관 손익 원장은 거래통화 금액과 기관 기준통화 환산액을 함께 저장하며, 건전성·예대율 계산은 서로 다른 통화의 숫자를 직접 합하지 않고 기관 기준통화로 환산한다. 회사 복식부기의 `financial_lines.amount`는 회사 통화의 **최소단위 정수**이므로, 예를 들어 USD 12.34는 1234로 저장한다. 이는 기준 통화 정수 회계가 아니다.
기존 외환통장의 이자 소수 잔액은 보존하고 화면에서만 통화별 자릿수로 표시한다.

## 회사 재무제표

### 조회

`/재무 product_id:CO` — 회사 코드 또는 우선주 코드를 직접 입력합니다.
우선주는 발행회사의 보고서를 엽니다. ETF·펀드·현물·외환·옵션은 회사 재무제표 대상이 아닙니다.
페이지: 메인, 재무상태표, 손익계산서, 현금흐름표, 자본변동표,
유동성·수익성·안정성·활동성비율, 주석. 월 선택은 최근 기록된 24개월과 현재 월입니다.
금액은 회사의 지정 통화와 소수점 규칙으로 표시하며 사용자 국적별로 재환산하지 않습니다.

### 관리자 명령어

모두 인자 없이 실행하면 기존 관리자 권한 확인 및 비공개 DM 흐름을 사용합니다.
서버에서는 명령어 메시지를 삭제하고 DM에서는 삭제하지 않습니다.

- `$재무설정`: 월간 영업 설정. 기본 비활성.
- `$재무계정`: 분개에 사용할 계정코드 목록.
- `$회계분개`: 회사 코드, 거래 설명, 계정/금액 JSON, 현금흐름 구분.
- `$재무정정`: 본인이 권한을 가진 회사의 관리자 분개 번호와 사유. 원본 삭제 없이 반대 분개.
- `$결산`: 회사 코드와 종료된 월 `YYYY-MM`. 마감 보고서는 변경하지 않습니다.
- `$재무검증`: 대차 균형, 현금흐름 대사, 현재 실제 법인현금 대사.

입력 시점에도 DB의 `role=admin`을 다시 확인합니다. 관리자 도움말의 재무·회계 카테고리에 포함됩니다.

#### 분개 예시

차변은 양수, 대변은 음수이며 합계가 0이어야 합니다. 모든 금액은 회사 기준통화입니다.

외상매출 (현금흐름 `none`):
```json
[{"account":"receivables","amount":1000},{"account":"revenue","amount":-1000}]
```

매출채권 수금 (현금흐름 `operating`):
```json
[{"account":"cash","amount":1000},{"account":"receivables","amount":-1000}]
```

설비 구입 (현금흐름 `investing`):
```json
[{"account":"ppe","amount":500},{"account":"cash","amount":-500}]
```

현금 분개는 실제 `issuer_cash_accounts.balance`와 함께 원자적으로 반영합니다.
현재 기존 법인현금 시스템이 기준 통화를 사용하므로 외화 법인의 현금 직접 분개는 제한합니다.
외화 법인의 매출채권·재고·비용 등 비현금 분개는 회사 통화의 소수점 단위로 지원합니다.
자동 연동 자본금·채권·배당채무·기초잔액·잠금예치금은 원본 상품/배당/발행 명령어로 처리합니다.

#### 월간 영업 설정

```json
{"enabled":true,"revenue":10000,"cogs":4000,"sga":1000,"depreciation":100,"tax_percent":10}
```

입력 월부터 적용합니다. 한국시간으로 해당 월이 끝나면 백그라운드 작업이 한 번 분개합니다.
매출은 매출채권, 원가·판관비는 매입채무로 인식합니다. 감가상각은 남은 유형자산 장부금액 이내로 제한합니다.
양의 세전이익에 설정 세율을 적용하고 세금을 미지급비용에 기록합니다.
현금 수금·지급은 별도 회계분개로 처리합니다. 주가로 매출을 추정하거나 자동 수금하지 않습니다.
다시 설정하면 현재 월부터 새 설정을 적용합니다. 과거 미처리 월을 소급 계산하려면 먼저 기존 설정으로 처리를 완료해야 합니다.
중단 기간 자동 소급은 하지 않습니다. 재시작 후 미처리 월은 한 번에 최대 24개월씩 처리하고 중복 인식을 방지합니다.

### 자동 연결

- 법인 잔고 이체: 현금 ↔ 자본잉여금.
- 최초 주식 판매: 실제 법인현금 또는 국고 보관 발행대금 ↔ 자본조정. 등록 시 이미 잡힌 자본금을 중복 인식하지 않습니다.
- 유상증자 체결: 현금/국고 보관 발행대금 ↔ 보통주·우선주자본금.
- 무상증자: 기존 게임은 자본금 변경 없는 수량 배분입니다. 현금/손익 분개 없이 유통주식수 이력을 갱신합니다.
- 배당 지급: 이익잉여금 감소. 국고가 대신 지급하면 자본 지원과 배당을 동시에 인식합니다.
- 누적 우선배당: 미지급배당 확정, 지급 시 채무 감소. 원래 수령자의 권리 유지.
- 회사채: 발행 부채와 발행차금, 만기 원리금, 발행차금 만기 상각. 기존 레거시 자본금 결제는 국고 보관 발행대금으로 구분합니다.
- ELS 발행회사: 예치금·예치부채 및 상환손익. ELS 상품 자체의 별도 회사 보고서를 만들지는 않습니다.
- 회사 합병: 자산·부채·자본 장부 인수. 피합병 회사 누적손익은 존속회사의 이익잉여금으로 이전합니다.
- 법인 청산: 잔여 장부 제거를 자본조정으로 표시합니다. 실제 청산금 지급은 기존 청산 기록에서 확인합니다.
- 보통주 유통량: 소수점 보유와 옵션담보 주식 포함, 국고 보유 제외. 거래·담보이동 시 이력을 저장합니다.

### 지표 산식과 범위

비율은 월간 손익 기준이며 연율화하지 않습니다. ROA·ROE·회전율은 기초·기말 평균잔액을 사용합니다.
도입 월처럼 기초자료가 없거나 분모가 0 이하인 경우 `산출 불가`로 표시합니다.
총자본은 총자산과 동일하게 정의합니다. 총자본세전이익률은 총자산세전이익률과 같은 값입니다.
당좌비율의 분자는 현금·단기금융자산·매출채권·국고 보관 발행대금입니다.
현금비율의 분자는 현금·단기금융자산입니다. 비현금 예치금은 포함하지 않습니다.
재고회전율은 매출원가/평균재고, 나머지 회전율은 매출액/평균해당자산입니다.
부채·유동부채·고정부채·고정비율의 분모는 자기자본입니다.
고정장기적합률은 비유동자산/(자기자본+비유동부채), 이자보상배율은 영업이익/이자비용입니다.

- EBITDA: 영업이익 + 감가상각비 + 무형자산상각비.
- EPS: (당기순이익 − 해당 월 우선주 확정배당) / 시간가중 보통주 유통주식수.
  도입 이전 주식수는 추정하지 않습니다. 최초 불완전 월은 미산출입니다. 희석 EPS·소급 주식분할 재작성은 미지원입니다.
- CAPEX: 투자활동으로 분류한 분개에서 유형·무형자산 취득에 사용한 현금.
- FCF: 영업현금흐름 − CAPEX.
- FFO/AFFO: 부동산 영업 모델이 없어 미지원.
- 기타포괄손익 미지원: 총포괄손익 = 당기순이익.

게임 회계로, K-IFRS 준수 보고서가 아닙니다. 회사채 이자는 만기 인식, 발행차금은 만기 일괄 상각합니다.
회사채의 만기별 유동/비유동 자동 재분류, 매출채권 대손, 연결재무제표, 환산차이 결산은 지원하지 않습니다.
기존 기준 통화 법인현금은 거래 시 환율로 회사 통화에 환산합니다. 외화 원장의 현금은 기말환율로 자동 재평가하지 않습니다.
기초 회사채·미지급배당·연결 우선주자본도 회사 기준통화로 환산해 이관합니다.

### DB와 이관

기존 `database/inazina.db`에 다음 테이블을 추가합니다.

| 테이블 | 용도 |
|---|---|
| financial_entities | 법인 기준통화·소수점·원장 도입일 |
| financial_accounts | 회계 계정코드·분류 |
| financial_entries | 거래 분개, 기간, 원본 키, 정정 대상 |
| financial_lines | 계정별 차변/대변 금액(정수 최소단위) |
| financial_periods | 결산 보고서 JSON 스냅샷 |
| financial_share_history | 보통주 유통수량의 시간 이력(1/1000주) |
| financial_operating_profiles | 선택적으로 활성화하는 월간 영업 설정 |
| financial_bond_basis | 만기 상각할 회사채 발행차금 |

다음 봇 시작 시 테이블 생성과 기존 활성 회사의 기초 잔액 이관을 실행합니다.
과거 매출·순이익은 생성하지 않습니다. 알려진 현금·자본·채무의 차액은 `도입 전 미확인 잔액`입니다.
이는 추정 자산도 추정 이익도 아니며 주석과 메인 경고에 표시합니다.
공시된 분개와 마감 보고서는 UPDATE/DELETE를 DB 트리거로 금지하고 정정 분개를 사용합니다.
실제 DB에 직접 잔고를 수정하면 원장과 다를 수 있으며 `$재무검증`에서 불일치를 확인할 수 있습니다.
일반 조회는 읽기 전용 WAL 스냅샷을 사용합니다. 마감 월은 저장된 보고서를 바로 읽습니다.

## 통화 금액 필드 인벤토리 (M4 종료 기준)

`financial_amount_field_registry`는 명시 분류된 금액 필드와 통화 선택식을 원천행이 0건이어도 등록한다. 뒤늦게 초기화되는 DB 모듈은 `register_late_amount_fields()`에서 추가 등록한다. `financial_amount_snapshots`는 각 원천행의 통화, scale, 최소단위 수치를 보존하며 `audit_amount_snapshots`가 등록 누락, snapshot 누락·불일치, 잘못된 원천 스키마를 보고한다. 별도 `audit_amount_field_coverage`는 초기화된 모든 SQLite 테이블의 숫자형 스키마를 이름 휴리스틱으로 검사하고, 레지스트리나 사유가 있는 명시 제외표에 없는 금액 후보를 통합 대사에서 mismatch로 표시한다. 해당 검사는 스키마 누락 탐지이지 소비자 코드까지 추적하는 데이터 계보 증명은 아니다. 생성 규칙은 [`currency_db.py`](../database/currency_db.py)의 `_ensure_minor_amount_snapshots`에 있다. 호환용 업무 열과 legacy 소비 경로가 남는 범위는 아래 잔여목록에 적으며, snapshot 존재 자체를 해당 업무 경로가 전부 정수 연산으로 바뀌었다는 뜻으로 보지 않는다.

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

### 잔여 호환 경로와 제한

#### 2026-10-24 통합 금액 스키마 감사 보강

초기화 순서 때문에 통화 DB 생성 시점에 존재하지 않았던 복권·기업행사·ELS 등의 테이블을 별도 등록 단계로 포함했다. 레거시 복합키 테이블의 `rowid`도 스냅샷 키로 감사한다. 숫자형 금액 후보를 레지스트리 또는 `financial_amount_field_exemptions` 사유표에 대조하고, 미분류 후보가 있으면 `reconcile_all_financial_ledgers` 전체 상태를 mismatch로 만든다. 명시 제외에는 지수 포인트·비율·점수·canonical 최소단위/호환 mirror만 포함하며 각 제외 이유를 DB에 보존한다. `market_events.target_value`는 비율과 통화 가격이 혼용되므로, 현재 실제 `price` 모드 이벤트가 있으면 미분류 금액 후보로 잡아 마감을 막는다. 이 동적 필드의 통화별 등록/스냅샷 규칙은 미완료다. 테스트는 초기 스키마의 미분류 후보 0건, 신규 `gross_amount` 열과 가격형 이벤트 후보의 검출을 확인한다. 이 감사는 고정된 DB 스키마의 분류를 강화하지만 업무 코드의 동적 SQL·표시·비정형 저장 경로까지 증명하지 않으므로, 아래 소비자별 잔여 제한과는 별개다.

#### 2026-10-20 초기 스키마 재점검 (아래 M4 종료 기록으로 갱신됨)

임시 DB를 새로 초기화하고 모든 테이블의 `PRAGMA table_info`를 순회해 금액 이름 패턴(`amount/balance/price/cost/fee/tax/cash/principal/income/revenue/proceeds/premium/capital/allowance/payment/deposit/dividend/claim`)과 REAL/float·INTEGER 저장형을 대조했다. 이는 스키마 필드 후보를 찾는 정적 휴리스틱이며, 모든 코드 소비자·런타임 migration 이후 데이터·웹/Discord 표시 경로를 증명하는 검사는 아니다.

- canonical 최소단위 대응을 갖춘 호환 REAL 필드: `bank_deficit_alerts.cash`; `depositary_receipt_movements.underlying_average_cost/holder_underlying_average_cost/custody_underlying_average_cost`; `dr_deviation_samples`·`dr_deviation_alert_events`의 `dr_price/reference_value`; `aml_alerts.amount`; `financial_institutions.equity_capital`; `accounts.reference_price`; `collateral_liquidation_events.execution_price`; `deposit_insurance_claims.payout_amount`; `market_trade_history.price`; `market_last_trade.price`; `financial_institution_ledger.amount/base_amount`. DR 이동 원가는 원주·보유자·수탁 통화를 각각 분리해 최소단위/scale을 스냅샷하며, 수탁 평균원가 소비는 canonical 정수 필드를 읽는다. 이 대응 열이 있다는 사실만으로 모든 소비자와 결제원장이 이관된 것은 아니다.
- `tax_transactions.gross_amount/tax_amount/net_amount`는 통화별 최소단위 정수 대응을 갖는다. 입력/update 경계의 native 통화 산식 `gross = tax + net`을 DB guard가 강제한다. 신규 양수 세금 행은 현재 일반소득 원천징수·배당·예금이자 경로에서 append-only `tax_transaction_wallet_links`를 통해 실제 국고 지갑 입금에 연결하고, 대사는 누락/비국고/비양수 영수증과 같은 통화일 때 세액 차이를 검출한다. 기존 데이터는 startup watermark로 재구성 불가 baseline에 격리한다. 외화 환산 시 원천통화 세액과 도착통화 간 FX fixing/변환 계산을 링크 자체가 재현하지 않으며, 시장 체결·채권 등 별도 세금 경로와 모든 국고·기관 분개 간 통합대사는 여전히 남아 있다.
- REAL이지만 금액이 아닌 것으로 분류한 후보: 금리·수수료율·배당률·비율·민감도, 시간(epoch), 수량(`deposited_underlying_quantity`), 유동성/시장 파라미터, 신용모형 점수·정책값. 시장 지수·기준 가격(`market_exchange_index_history.index_value`, `market_simulation_state.latent_price`, `market_auto_targets.anchor_price/target_price`, `market_event_targets.*_price`, `fund_components.base_price`, `els_underlyings.initial_price/last_price`)은 통화 현금은 아니지만 가격표시 단위 및 quote currency를 명확히 할 필요가 있다.
- INTEGER로 저장되지만 스키마명만으로 전용 최소단위 권위 필드를 확인할 수 없었던 통화 금액 후보에는 고객 현금/연소득, 상품 예치/상환액, `user_assets.average_cost`, 옵션 strike/premium/collateral, 펀드 현금원장, 회사자본/배당, 대출 수수료·담보, 예금보험 한도/청구, 회사청산·DR 배당·ELS 지급, 복권 금액, 송금 수수료가 포함된다. 일부는 기준 통화 whole-unit 전용, 별도 원장 또는 snapshot으로 관리되므로 이름만으로 이관 여부를 단정할 수 없다. 업무별 quote currency와 권위열·소비자를 인벤토리에 매핑해야 한다.

이 초기 패스는 REAL/INTEGER 후보를 모은 상태라 당시에는 M4 첫째 게이트가 미완료였다. 이후 명시 금액 필드 레지스트리와 최소단위 스냅샷 감사가 추가됐다. 아래 소비자별 제한은 최종 M4 종료 기록의 범위를 보충한다.

#### 시장 지정가 추적 (확인 범위: 표준 자산 주문, 미완료)

`/주문`·웹/Discord 주문 진입은 `OrderBookManager.place_limit_order`로 모이고, `market_orders.limit_price_minor_units`가 quote 통화 최소단위 권위값이다. 입력 파서는 상품 통화 정밀도를 검증하고 표준 매처의 가격·우선순위·예치금·환불·1차/2차 체결 정산은 그 정수값을 읽는다. 고객 현금은 `cash_minor`로 차감/입금하고, 계좌 micro-unit 호환 경로도 최소단위에 정렬된 정수만 증감한다. 옵션·선물은 기준통화(0자리) 전용 상품이고 국채 및 소수 통화 fractional 주문은 상품 통화 정밀도를 사용한다. 웹/Discord의 legacy 표시 열은 호환 출력 미러다. 전사 공통 전기와 clearing 대사는 M4 Step 3 이후의 남은 범위다.

- 애플리케이션 소비자는 아직 여러 곳에서 `REAL`/`float` 원본값과 계좌 micro-unit(`balance_units`)을 직접 사용한다. 세금 총액/세액/순액, 대출 원금/잔액, 충당금, 상각 메모잔액, 상환예정/납부 구성요소, 시장 주요 가격/체결 및 국채 발행/보유 금액은 통화 최소단위 필드와 호환 동기화·일부 불변식을 제공하지만, 일부 업무 소비자는 여전히 레거시 수치 열을 읽는다. 나머지 이자·수수료·외환환산·상품/NAV·채권 평가 및 소비자 경로는 계속 이관 대상이며 스냅샷만 등록된 금액은 권위 업무필드를 대체하지 않는다.
- 모든 필드의 원천 이벤트·중간 계산·DB 저장·웹/Discord 표시 소비자를 연결해 이 인벤토리와 대조하는 전사 데이터 계보 테스트가 남아 있다.
- 대출 정상·연체 이자 잔여는 통화 최소단위의 1/1,000,000 단위 정수로 보존하고, 기존 REAL 열은 호환용이다. 이는 발생 잔여의 정밀도 보완이며 이자 계산 전 과정이나 대출 원장 전체의 Decimal/정수 이관은 아니다.
- 정상 이자 발생, 연체이자 및 변동금리 재산정은 `outstanding_balance_minor_units` 기준으로 읽고 발생 이자를 최소단위 정수로 누적한다. 단리/복리 금액 산식과 ACT/360·ACT/365 분율은 Decimal이며 subminor 잔여도 정수로 보존한다. 30/360 날짜 경계 및 담보 재평가/회수 등 다른 소비자와 일부 계약 원금·잔액은 여전히 기존 float/REAL 경로여서 대출 계산 전체가 canonical화된 것은 아니다.
- 변동금리 재산정 이자는 기관 손익 원장, BankJournal 및 공통원장에 같은 참조키로 연결하고 최신 대출 커서를 재조회해 중복 발생을 방지한다. 대출 전체 생애주기 분개와 전사 대사가 완료된 것은 아니다.
- 정기 상환의 회차 예정액·미납액·잔액 차감은 이제 상환표/대출의 통화 최소단위 정수 열을 우선 사용한다. 기존 소비자 경계로 내보낼 때만 해당 통화 금액으로 변환한다. 레거시 상환과 이자 산출 등 대출 전체가 이관된 것은 아니다.
- 레거시 주기상환도 약정 상환액·대출 잔액을 통화 최소단위로 변환해 정수 연산하고, 자금 차감·기관 분개·잔액 변경에 동일 금액을 사용한다. 다른 대출 금액 경로와 회사/은행 전체 현금흐름 대사는 여전히 미완료다.
- 수동/중도상환은 대출 잔액·지급액·중도상환수수료·충당금 회수액·총 고객 차감을 계약통화 최소단위 정수로 제한해 잔액 변경 및 BankJournal 분개에 같은 금액을 사용한다. ECL/provisioned-loss의 모든 경로와 원장 전체 현금흐름 대사까지 완료됐다는 뜻은 아니다.
- 고객 지갑과 공통원장 대사는 이제 각 이벤트의 통화/정밀도, 기록시각, 매핑별 차변·대변 및 금액을 직접 비교한다. 은행·회사·시장 거래 전체의 전사 지급 연결은 별도 미완료다.
- 2026-10-24 표준 및 외화 증권 매칭 후속: 표준 `OrderBookManager`도 지정가 parser, maker 가격 우선순위, 가격 기준 발행·체결, 가격개선 환급, 매도손익·세금·순대금을 quote 통화 최소단위로 계산한다. 신규 홈통화 현금 차감/입금은 `cash_minor`를 권위값으로 사용하고 일반 계좌 잔액은 최소단위에 정렬된 micro-unit 정수만 변경해 canonical 잔액·지갑 트리거를 발생시킨다. 보유원가와 특수 상품 가격 경계는 2026-10-30 후속으로 보강했다. 표시 및 전사 clearing·공통 전기·전체 보조원장 대사는 여전히 별도 미완료 범위다.
- 미체결 매수 주문 예치금은 `reserved_cash_minor_units` 정수 권위값을 사용한다. 기존 주문은 quote currency 정밀도로 이관하며 부분체결 때 지정가×체결수량을 최소단위 정수로 차감하고, 취소·예약거절 환불도 이 값에서 복원한다. 신규 주문의 실제 현금 예치/환불 wallet ledger 항목을 `market_order_wallet_links`에 연결하며, 연결된 순차감액 = 체결 총액 + 잔여 예치액을 `market_order_wallet_cash` 대사로 검사한다. 과거 주문은 마이그레이션 시점 baseline으로 격리한다. 표준·외화 현물 매칭은 canonical 최소단위로 연결됐으나 옵션·선물·분할주 및 채권 매처의 예약·체결금액과 전사 clearing은 아직 미완료다.
- `AccountingJournal.reconcile_subledgers`는 표준 자산 매수 주문의 활성 예약 잔액이 지정가 최소단위×잔량과 일치하는지 점검하고, 부분체결/종결 주문의 과다·누락 예약을 finding으로 노출한다. 이는 주문 예약 내부 대사이며 고객 현금, 체결대금, 증권 보유, 기관/회사 장부 간 결제 대사는 아니다.
- 신규 표준 현물 매칭은 양측 주문/당사자와 quote-currency 최소단위 가격·총액·세금·순대금을 불변 `market_execution_events`에 기록하고 공통 market journal에 균형 전기한다. 주문별 기존 체결량 baseline 이후의 증분 체결수량 및 이벤트별 공통 분개를 대사한다. 체결 처리 중 실제 생성된 고객 지갑 원장 entry를 `market_execution_wallet_links`에 불변 연결하고, 참조 무결성·통화/정밀도 및 매도자 순수령액을 체결 이벤트와 대사한다. 예약 시점 매수금 인출, 보유증권 이동, 발행자 결제, 옵션·선물·분할주 매처와 clearing의 전사 연결은 남아 있다.
- 국채 쿠폰·만기상환은 통화 최소단위의 불변 지급 이벤트를 생성하고 같은 트랜잭션에서 `treasury_bond` 공통원장에 전기한다. 신규 이벤트는 실제 고객 지갑 입금 또는 펀드 현금 입금과 불변 링크로 연결되며 수취인·금액·통화/scale 차이를 대사한다. 과거 이벤트는 시작 watermark로 격리한다. 기준 통화 국채 지급이 타국 고객의 국내통화 cash 계정에 같은 숫자로 기록되는 기존 경로가 있어 대사는 mismatch를 노출한다. 이 경우 FX 환산·clearing은 아직 미구현이다. 2026-10-30 후속으로 국채 1차 매수와 2차 이전의 액면·원금·취득원가·세금·수취액 계산은 quote 통화 최소단위를 사용한다. 발행자/국고·고객 현금·증권 간 전사 clearing 대사는 남아 있다.
- 예금보험 청구의 지급 통화 금액은 `payout_amount_minor_units`와 당시 `payout_decimal_places`를 권위값으로 저장한다. 신규 insert는 통화 scale·레거시 mirror와의 일치를 DB trigger로 강제하며, 기존 통화/금액이 유효한 행은 startup migration에서 backfill한다. 사용자 API는 정수 단위에서 표시값을 복원하고 기관 대사는 payout 보조원장의 합계 및 미확인/미러 불일치를 검사한다. 레거시 REAL 지급액은 읽기 호환 미러로 남고, 개별 지급을 고객 지갑/국고/환전 거래 ID와 직접 연결하는 전사 지급 대사 및 알 수 없는 과거 통화의 수동 정정 절차는 미완료다.
- 담보자산의 원가·최근가격·평가액·청산가격·청산비용/대금 및 청산 이벤트 기준가격·체결가격·총/순대금에 통화 최소단위 정수 필드를 연결했다. 레거시 청산행은 대출 계약통화로 backfill하고, mutable 담보 잔액은 insert/update 호환 trigger, 불변 청산 이벤트 가격은 insert 시 DB trigger 및 대사에서 legacy mirror와 검증한다. 담보 재평가는 가격·헤어컷 적용 평가액을 Decimal→최소단위 정수로 산출한다. 이는 담보 가격·평가 경로의 보강이며 담보 매각 전 과정의 고객/은행 현금·증권 원장 전사 결제 대사는 미완료다.
- IMA 만기정산의 기준가격도 계좌 계약통화 기준 `reference_price_minor_units`를 권위값으로 저장·backfill하고 레거시 금액은 최소단위 mirror로 정규화한다. 만기 수익률은 현재 기준가격과 최초 기준가격을 모두 통화 최소단위 정수에서 계산한다. 기준 통화 기준 IMA 만기/은행손익 테스트가 통과했다. 다만 환산·현금 지급 분개 전체 경로는 별도 미완료다.
- 금융기관의 자기자본(`equity_capital`)도 기관 국적의 통화 및 정밀도 스냅샷과 `equity_capital_minor_units`를 저장하고, 기존 레거시 값 backfill/양방향 미러를 제공한다. 기관 자본 컴포넌트와 국가별 자본집계는 정수단위 원본에서 읽는다. 기관 자본 변경, 상품 심사 영향 및 기존 기관 ledger migration 대상 테스트가 통과했다. 관할 통화 자체가 변경될 때의 재표시/환산 회계와 여러 기관 통화 간 연결 대사는 남아 있다.
- AML 모니터링 alert의 금액은 통화별 최소단위 `amount_minor_units`와 정밀도 snapshot을 보유하며 기존 alert backfill·insert/update 호환·canonical display를 적용했다. sender/recipient가 각자의 국내통화로 금액을 산출하는 테스트와 compliance overview API 테스트가 통과했다. 원거래 FX fixing 자체를 경보에 스냅샷하고 지급원장까지 연결하는 AML 경보 전사 대사는 별도 미완료다.
- DR deviation sample/alert의 거래가격 및 환산 reference price도 DR 상장통화 최소단위 정수 필드에 고정하고 과거 표본 backfill 및 history API 노출을 추가했다. DR 원천징수 이벤트도 지급 통화 `decimal_places`, `gross_minor_units`, `tax_minor_units`, `net_minor_units`로 이관하고, USD 소수 통화의 과거 이벤트 backfill 및 `gross=tax+net`·지급 snapshot 대사를 추가했다. DR 프리미엄/할인 전이·버킷 중복·보존기한 및 DR 세금 테스트에서 금액 snapshot을 확인한다. underlying 통화→DR 통화 환산을 구성한 세부 fixing/시장가격 원천까지 재현하는 event snapshot은 별도 미완료다.
- 통화·자산 유형별로 빠진 금액 필드가 없는지 스키마 전체 정밀 검토를 해야 한다. 특히 옵션/파생, 미결제·예약, 수탁 지급, 배당 청구, 법인/펀드 보조원장과 과거 가격 기록의 누락 검증이 필요하다.
- 고객 지갑은 신규 현금/계좌 잔액을 공통 journal에 자동 전기한다. 원천징수 세금은 신규 국고 지갑 입금과 불변 연결하고, 과거 세금행은 watermark baseline으로 분리한다. 배당 지급은 고객 wallet 또는 펀드 현금 ledger의 실제 양수 행 하나에 불변 링크하며, 신규 레코드 대사에서 수취자·통화·scale·최소단위 금액/펀드 event를 검사한다. 펀드 기준 통화 현금 ledger도 공통 journal에 균형 전기·재실행 backfill·잔액 대사를 연결했다. 하지만 이 연결은 전사 자동 전기의 완료를 뜻하지 않는다. 상품 NAV/보유자산, 지급재원·회사손익, 세금 원천별 FX clearing 및 전체 보조원장 대사, 전사 과거 잔액 재구성, 부분 실패·재실행·기간 마감/정정 통합 검증은 여전히 미완료다.
- `AccountingJournal.trial_balance`는 원장/주체/통화 및 선택적 `[시작, 종료)` epoch 범위로 공통원장 계정별 차변·대변·순잔액을 상품통화 최소단위로 합산하고, 통화 scale snapshot 불일치를 거부한다. 이는 조회 도구이며 미연결 보조원장이나 빈/불균형 분개를 스스로 보정하지 않는다. 전체 회계범위 대사와 별도 사용해야 한다.
- 신규 표준 현물 체결은 `market_execution_holding_movements`에 실제 수량 이동을 거래별 정수로 보존한다. 일반 자산은 `user_assets.quantity`, 국채는 활성 `treasury_bond_holdings`의 액면 단위 수량과 비교하며, 1차 발행은 매수자 증가, 2차 매매는 매수자 증가·매도자 감소를 검증한다. 기존 체결은 별도 baseline으로 격리한다. 증권 이동 원장의 커버리지는 비분할 표준 현물에 한정되고 발행자 결제/clearing과 기타 매처의 전사 연결은 미완료다.
- 체결량 대사 baseline은 주문 현재 체결량과 이관 시점 이벤트 누계를 함께 1회 저장한다. 재기동 때 신규 주문이나 체결을 뒤늦게 baseline으로 넘기지 않으며, 주문 체결량 변화와 이벤트 누계 변화량을 비교한다.
- 신규 1차 발행은 상장국 국고/발행자 계정의 실제 고객지갑 수취 링크를 체결 총액·통화·정밀도와 비교한다. 발행자 현금계정이 고객지갑 원장에 연결되지 않거나 cross-currency settlement가 있으면 finding으로 노출한다. 이는 발행자 현금잔액의 독립 원천이벤트/복식원장 또는 FX fixing·clearing을 대체하지 않는다.
- 회사채 `user_assets` 잔액 × 액면을 발행회사 재무원장의 `bonds`/`bond_discount` 순액과 대조한다. 회사 원장 미개설, 회사통화와 다른 채권통화, 발행 후 장부 변화를 finding으로 보인다. 신규 동일통화 1차 발행은 보유량·현금 변경 전 opening snapshot을 만들고 발행대금·액면·발행차금을 하나의 멱등 회사 분개로 기록한다. 쿠폰과 만기상환도 같은통화 회사 분개로 연결한다. 외화 발행, 과거 개별 이벤트 재생은 baseline/제한에 남는다.
- 같은통화 정기 쿠폰과 만기상환은 기존 보유부채를 원장에 opening 재구성하고, 지급 성공 시 총이자비용·현금 감소 및 만기 액면부채 제거를 멱등 회사 분개로 연결한다. 만기 발행차금 상각도 상환에 앞서 기록한다. 외화 fixing과 거래별 역사 재생은 별도 제한이다.
- 회사채 대사에서 SQLite `SUM(quantity * face_value)`의 REAL 승격을 피한다. 보유 수량은 정수로 합산하고 액면 곱셈과 통화 최소단위 변환은 Python `Decimal` 경계에서 수행한다. 이는 대사 계산의 정밀도 개선이며 회사채 금액 원천·저장 경로 자체를 이관한 것은 아니다.
- 2026-10-30 M4 Step 2 완료 기록: 선물 증거금은 Decimal 기반 통화 최소단위 올림, 실현손익 배분은 정수 비례연산을 사용한다. 회사채 쿠폰은 day-count 비율 및 이자액을 Decimal로 계산한다. 대출 현금흐름은 minor-unit 원금에서 Decimal 금리/일수계산을 유지하고, 담보 청산·ECL allowance도 canonical units를 사용한다. 국채 1차·2차 거래와 fractional 외화 지정가/손익도 상품 통화 최소단위로 계산한다. 대출·옵션·선물 일부 상품의 기준통화 제한은 명시된 지원 규칙이다. 레거시 SQL/표시 API REAL 열은 호환 미러로 남는다. 고객·기관·회사 원장과의 전체 공통 전기·clearing은 다음 M4 단계다.
- 2026-10-30 보유원가 후속: `user_assets.average_cost_minor_units`를 권위값으로 사용하도록 매수·주식 이동·담보 반환·시장 청산·법인 자본행사·DR 전환/상환·ELS 취득의 가중원가 계산을 `AssetCostManager`에 모았다. 일반/외화/직접/소수점 현물 세금 원가 및 회사·펀드·그룹 재무 대사는 canonical 최소단위를 읽는다. `average_cost` REAL은 호환 표시 미러다. 영향 테스트 및 제한은 realism plan 검증 기록에 적는다.

## 개인 신용점수

`users.credit_score`(0~1000)는 `managers/credit_score_manager.py`가 계산해 저장하는 캐시입니다. 점수는 불변 이벤트 원장과 현재 대출 상태로 언제든 다시 계산할 수 있고, 직접 덮어쓰지 않습니다. 가중치는 실제 신용평가사 점수표의 형태(상환이력 비중이 가장 큼)를 따르지만, 보정된 실제 모형이 아니라 `credit_score_policy`에 있는 시뮬레이터 가정입니다.

### 점수 = 기준점수 + 요인 합계

| 요인 | 근거 | 기본값 |
|---|---|---|
| 정상 납부 실적 | `payment_on_time` 이벤트 | 회당 +4, 반감기 60일, 상한 +120 |
| 대출 완납 실적 | `loan_closed` 이벤트 | 건당 +20, 반감기 90일, 상한 +60 |
| 납부 연체 기록 | `payment_missed` 이벤트 | 회당 −35, 반감기 14일, 90일 후 소멸 |
| 연체 회차 사후 납부 | `payment_cured` 이벤트 | 회당 +15(연체 기록과 같은 감쇠) |
| 기한이익상실 기록 | `acceleration` 이벤트 | −120, 반감기 30일, 180일 후 소멸 |
| 부도 기록 | `default` 이벤트 | −250, 반감기 60일, 180일 후 소멸 |
| 현재 미납 회차 | 미상환 대출의 `missed` 회차 수 | 건당 −20, 상한 −100. 갚으면 즉시 소멸 |
| 현재 연체 중 대출 | 만기 경과·기한이익상실·부도 상태 잔액 | −60 / −150 / −250, 상한 −400. 상환 시 즉시 소멸 |
| 소득 대비 부채수준 | 미상환 대출(국적 통화 환산) ÷ (연소득 × 소득 안정성) | 0.2 이하 +15 → 1.0에서 −60 → 초과 1.0당 −40, 하한 −150. 소득 미등록이면 0 |
| 신용거래기간 | 가장 오래된 대출 개설일 | 180일에 걸쳐 최대 +40 |
| 최근 신규 대출 | 14일 내 개설 대출 수 | 두 번째 대출부터 건당 −10, 상한 −30 |
| 관리자 정정 | `manual_adjustment`, `external_adjustment` | 영구 |
| 도입 전 점수 보정 | `legacy_baseline` | 모델보다 낮은 차이는 반감기 30일로 감쇠, 높은 차이는 유지 |

기준점수는 700입니다. 결과는 반올림 후 0~1000으로 자릅니다.

### 동작

- **도입과 기존 점수**: 사용자를 처음 계산할 때 `현재 점수 − 모델 점수`를 `legacy_baseline`으로 기록하므로 도입 시점에 점수가 바뀌지 않습니다. 이전 시스템은 연체 감점으로만 점수를 낮추고 관리자만 점수를 올렸으므로, 모델보다 낮은 차이(도입 전 연체 흔적)는 30일마다 절반씩 줄고 모델보다 높은 차이(관리자가 부여한 점수)는 영구 정정처럼 유지합니다.
- **회복**: 연체 기록은 반감기에 따라 줄고 보존기간이 지나면 사라집니다. 미납 회차·연체 잔액을 갚으면 현재 연체 감점은 즉시 사라지고, 정상 납부·완납 실적이 가점을 쌓습니다.
- **수동 정정**: 관리자 화면·API에서 신용점수를 바꾸면 목표 점수와 모델의 차이를 사유·처리자와 함께 `manual_adjustment`로 기록합니다. 정정 후에도 점수는 새 이벤트에 따라 계속 움직입니다. SQL 등 모델 밖에서 값을 바꾸면 다음 계산 때 `external_adjustment`로 흡수됩니다.
- **기록 시각**: 연체는 회차 납기, 기한이익상실·부도는 만기 + 상품의 경과일로 기록합니다. 정산이 늦게 돌아도 계약상 발생일이 남습니다.
- **정기 작업**: 봇 대출 워커가 10분마다 만기 경과 대출을 정산하고(기한이익상실·부도 판정이 계정 접근을 기다리지 않음), `refresh_interval_seconds`(기본 1시간)보다 오래된 점수를 감쇠 반영해 다시 계산합니다.
- **기록 보존**: `credit_events`, `credit_score_history`는 수정·삭제가 막힌 append-only 테이블입니다. 이벤트는 (사용자, 종류, 참조) 단위로 한 번만 기록됩니다.

### 조회

- 관리자: 대시보드 사용자 상세의 신용점수 카드(요인, 이벤트, 점수 변경 이력), `GET /internal/admin/user/<id>`의 `credit`.
- 본인: HTS 지갑의 신용점수 패널(요인과 감점 요인별 회복 방법, 최근 이벤트), Discord 지갑의 신용 점수 칸(가장 큰 감점 요인), `GET /api/me/credit` → `GET /internal/me/credit`.

### 조정

기간은 실제 일수이며, 실제 신용평가의 연 단위 기간을 시뮬레이터 속도에 맞춰 주~월 단위로 줄인 값입니다. 더 조정하려면 `credit_score_policy`의 `*_half_life_days`, `*_retention_days` 등을 SQL 편집으로 바꿉니다. 이전 연 단위 기본값이 남은 행은 시작 시 새 기본값으로 옮기고, 관리자가 바꾼 값은 유지합니다. 바꾼 값은 과거 이벤트에도 다음 계산부터 적용됩니다.

### 남은 범위

법정 연체정보 등록·해제 기준, 단기연체(예: 5영업일 미만) 비반영, 카드·보증 등 다른 신용형태, 조회 기록, 공동 차주·보증인 전이, 연체 금액 크기 반영은 아직 모델링하지 않습니다.
