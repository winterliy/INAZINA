# 화면 설계 (디스코드·HTS)

디스코드 명령 화면의 동작 규칙과 HTS 웹 화면 설계를 모았다.

- [디스코드 지갑·검색·그래프·대출 화면](#디스코드-지갑검색그래프대출-화면)
- [HTS 차트 그리기 도구](#hts-차트-그리기-도구)
- [HTS 페이지·게시판 개편 설계](#hts-페이지게시판-개편-설계)

## 디스코드 지갑·검색·그래프·대출 화면

`/내지갑`과 국가 명의 `$내지갑`의 메인은 현금·통장·투자 평가액·옵션 의무·총자산·부채·순자산·신용점수의 상단 정보 박스만 표시한다. 상세 보유 내역은 카테고리를 선택해서 확인하며, 상세 카테고리에는 메인의 요약 박스를 반복 표시하지 않는다. `$내옵션`과 `$내자산`은 사용하지 않는다.

`/검색`의 거래소 지수 카테고리에는 현재 지수만 표시하고 `__exchange_index__:` 내부 ID는 노출하지 않는다. 거래대금·거래량·급상승·급하락 실시간차트와 총자산 상위 20명(페이지당 5명)을 카테고리로 제공한다. 금리 카테고리에는 국적별 기준금리와 법정최고이자가 표시된다. 그래프 종목에 `기준금리`, `법정최고이자`를 직접 입력할 수도 있다. 우선주는 주식 목록에 우선주 표식을 보이고 의결권이 없는 상품으로 안내한다. 정기배당이 설정된 주식·상품은 주기와 주·좌당 배당금을 표시한다.

그래프의 시간 선택은 캔들·집계 주기다. 봉이 겹치지 않도록 주기마다 X축 총 표시 기간을 다르게 적용한다. 1분봉은 10분, 5분봉은 1시간, 10분봉은 6시간, 30분봉은 12시간을 표시하고 1시간봉·6시간봉·12시간봉·일봉은 1일을 표시한다. 일목균형표 미래 구름은 이 표시 범위를 늘리지 않으며, 그보다 이전 기록은 지표 계산 준비 구간으로만 사용한다.

`/대출신청 id:INZLN001`처럼 대출 상품 ID를 입력한 뒤 버튼의 금액 입력 창에서 신청한다. 검색 화면의 대출 신청 버튼도 상품 ID와 금액을 직접 입력한다. `/대출연장 id:13`은 `/내지갑` → 대출에 표시된 개인 대출 `#13`을 연장한다. 대출 상품의 연장 신용 기준과 허용 기간은 검색 화면의 대출 상품 목록에 표시되며, 등록되지 않은 연장 기간은 연장 불가로 안내한다. 국가 명의 기능은 `$대출연장 13`으로 실행한다.

## HTS 차트 그리기 도구

The HTS chart should support cursor/selection, trend lines, and horizontal price levels. Users draw a trend line by dragging and place a horizontal line by clicking a price. Drawings can be selected, moved, deleted, undone, and cleared.

In the real HTS, persist drawing anchors as chart time and price values, not screen pixels. Drawings must stay attached to the same time and price when users resize, zoom, pan, or change candle intervals. Scope saved drawings by authenticated user and instrument.

## HTS 페이지·게시판 개편 설계

### Intent

Make the HTS easier to navigate by giving command workflows and account management their own pages, improving chart context, and presenting the board as a familiar Korean online community. Keep the existing trading, account, and board APIs and user data working.

### Goals

- Replace the command modal with distinct pages and URLs for each command function, with navigation between related command pages.
- Add a lower volume panel and useful auxiliary chart views beneath price, sharing the selected instrument and time range.
- Rebuild the board page layout around a DCinside-style gallery: board navigation, category tabs, notice and popular-post emphasis, compact post rows, search, pagination, and a readable post view. Preserve current board APIs and capabilities such as writing, comments, polls, attachments, moderation, and asset filtering.
- Create a dedicated account page for account identity/settings and account-related information.
- Move the credit score summary and its factors/events into the wallet area; remove the separate credit block from the bottom of the main HTS page.
- In the administrator dashboard, organize command selection and command cards by the function categories already defined in `COMMAND_FORMS`.
- Replace the dashboard's local-console footer with a short copyright line and show the same line at the bottom of HTS pages.
- Keep the experience usable on mobile and retain authentication and return-to-page behavior.

### Non-goals

- No backend or schema changes unless implementation proves an existing API cannot support the requested pages.
- No importing DCinside assets or copying its branding; use its information hierarchy and compact forum conventions as reference.
- No change to financial calculations or command semantics.

### Approaches considered

1. **Separate command pages (chosen):** each command function has a direct URL, with shared navigation and existing command execution behavior. This matches the explicit request and makes pages linkable/bookmarkable.
2. **Command center with tabs:** one route with a category sidebar. It would be cheaper but would retain the bundled-page model the user asked to remove.

For the board, preserve the existing page and replace its visual structure, rather than changing APIs or introducing a new board backend. For charts, add auxiliary SVG panels to the existing chart component so they share its data, time scale, and interactions.

### Constraints and implementation calls

- Existing app routing is selected in `main.tsx`; `/board` is already a standalone page. Add account and command route handling there, with a safe fallback to the main HTS.
- `CommandPanel` currently owns command definitions, forms, results, and execution. Reuse/refactor that source of behavior into page-sized command screens instead of reimplementing command requests.
- `PriceChart` currently renders SVG price data and saved drawings. Reuse its `candles` and `points` props to derive volume bars and auxiliary series; show an explicit empty state where source data is unavailable.
- The account identity editor is currently a modal in `App.tsx`; move it into the account page. Wallet and credit data already load from existing account APIs.
- The administrator dashboard already maps each command to a function category through `COMMAND_FORMS`; use these groups as its category tabs.
- Board API and data logic live in `BoardPanel.tsx`; retain these behaviors while replacing its page layout and styles.
- Visual direction uses DCinside conventions (gallery header/tabs, compact table columns, clear notice/popular labels) without a pixel-for-pixel clone. The current reference emphasizes gallery tabs and list columns for number, category, title, author, date, views, and recommendations. [Reference: DCinside production guide](https://nstatic.dcinside.com/ad/2025/pptfile/2025_dcinside_production_guide.pdf)

### Acceptance

- Every existing command is reachable on a function-specific URL and executes with its current inputs/results.
- The main chart has a price region and at least one lower volume graph; auxiliary indicators render when their required data exists.
- `/board` reads and writes through the existing APIs in the redesigned community layout.
- `/account` (or the chosen account route) is directly navigable and contains the former account identity settings.
- Credit score appears within the wallet area and is absent as a separate bottom-page card.
- Existing navigation, login, mobile layout, and deep links remain coherent.
