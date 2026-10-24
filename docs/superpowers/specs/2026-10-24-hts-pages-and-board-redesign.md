# HTS pages and board redesign

## Intent

Make the HTS easier to navigate by giving command workflows and account management their own pages, improving chart context, and presenting the board as a familiar Korean online community. Keep the existing trading, account, and board APIs and user data working.

## Goals

- Replace the command modal with distinct pages and URLs for each command function, with navigation between related command pages.
- Add a lower volume panel and useful auxiliary chart views beneath price, sharing the selected instrument and time range.
- Rebuild the board page layout around a DCinside-style gallery: board navigation, category tabs, notice and popular-post emphasis, compact post rows, search, pagination, and a readable post view. Preserve current board APIs and capabilities such as writing, comments, polls, attachments, moderation, and asset filtering.
- Create a dedicated account page for account identity/settings and account-related information.
- Move the credit score summary and its factors/events into the wallet area; remove the separate credit block from the bottom of the main HTS page.
- In the administrator dashboard, organize command selection and command cards by the function categories already defined in `COMMAND_FORMS`.
- Replace the dashboard's local-console footer with a short copyright line and show the same line at the bottom of HTS pages.
- Keep the experience usable on mobile and retain authentication and return-to-page behavior.

## Non-goals

- No backend or schema changes unless implementation proves an existing API cannot support the requested pages.
- No importing DCinside assets or copying its branding; use its information hierarchy and compact forum conventions as reference.
- No change to financial calculations or command semantics.

## Approaches considered

1. **Separate command pages (chosen):** each command function has a direct URL, with shared navigation and existing command execution behavior. This matches the explicit request and makes pages linkable/bookmarkable.
2. **Command center with tabs:** one route with a category sidebar. It would be cheaper but would retain the bundled-page model the user asked to remove.

For the board, preserve the existing page and replace its visual structure, rather than changing APIs or introducing a new board backend. For charts, add auxiliary SVG panels to the existing chart component so they share its data, time scale, and interactions.

## Constraints and implementation calls

- Existing app routing is selected in `main.tsx`; `/board` is already a standalone page. Add account and command route handling there, with a safe fallback to the main HTS.
- `CommandPanel` currently owns command definitions, forms, results, and execution. Reuse/refactor that source of behavior into page-sized command screens instead of reimplementing command requests.
- `PriceChart` currently renders SVG price data and saved drawings. Reuse its `candles` and `points` props to derive volume bars and auxiliary series; show an explicit empty state where source data is unavailable.
- The account identity editor is currently a modal in `App.tsx`; move it into the account page. Wallet and credit data already load from existing account APIs.
- The administrator dashboard already maps each command to a function category through `COMMAND_FORMS`; use these groups as its category tabs.
- Board API and data logic live in `BoardPanel.tsx`; retain these behaviors while replacing its page layout and styles.
- Visual direction uses DCinside conventions (gallery header/tabs, compact table columns, clear notice/popular labels) without a pixel-for-pixel clone. The current reference emphasizes gallery tabs and list columns for number, category, title, author, date, views, and recommendations. [Reference: DCinside production guide](https://nstatic.dcinside.com/ad/2025/pptfile/2025_dcinside_production_guide.pdf)

## Acceptance

- Every existing command is reachable on a function-specific URL and executes with its current inputs/results.
- The main chart has a price region and at least one lower volume graph; auxiliary indicators render when their required data exists.
- `/board` reads and writes through the existing APIs in the redesigned community layout.
- `/account` (or the chosen account route) is directly navigable and contains the former account identity settings.
- Credit score appears within the wallet area and is absent as a separate bottom-page card.
- Existing navigation, login, mobile layout, and deep links remain coherent.
